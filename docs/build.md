# Building SSIS projects without Visual Studio (`onprem-ssis build`)

```bash
onprem-ssis build STAGE/STAGE.dtproj                        # -> STAGE/bin/Development/STAGE.ispac
onprem-ssis build STAGE/STAGE.dtproj -c Release --out x.ispac
onprem-ssis deploy-project --env dev --folder STAGE --project STAGE --dtproj STAGE/STAGE.dtproj --only Load_Customer
onprem-ssis deploy-project --env dev --folder STAGE --project STAGE --dtproj STAGE/STAGE.dtproj --all
```

`build` turns a Visual Studio SSIS project (project deployment model) into the `.ispac` that
Visual Studio's Build would produce, on any machine with Python: no Visual Studio, no SSIS
runtime, no Windows. `deploy-project --dtproj` builds in memory and deploys from that.

## What Visual Studio's build does, and what `build` does

Measured in the lab with Visual Studio 2022 + SSIS Projects 2.2 (`devenv.com /Build`) and
SSISBuild.exe 1.0, on projects created with the SSIS object model and stored the way Visual
Studio stores them (`lab/tools/build-corpus.ps1`; the projects and both builds are the test
fixtures in `tests/fixtures/build/`).

| Step | Visual Studio | SSISBuild.exe | `onprem-ssis build` |
|---|---|---|---|
| Manifest | from the `.dtproj`, regenerated | from the `.dtproj` as stored | from the `.dtproj` |
| Package metadata in the manifest (description, version, protection, package parameters) | refreshed from each `.dtsx` | **left stale** if a package was edited outside Visual Studio (a merge, a text editor) | refreshed from each `.dtsx`, and reported in `refreshed_metadata` |
| Configuration values (`Development`, `Release`, ...) | written into `Project.params` | written into `Project.params` | written into `Project.params` (`Project::` values) |
| Packages | **re-saved** with Visual Studio's own SSIS runtime: newer version stamp, extra connection properties (`ConnectRetryCount`, `ConnectRetryInterval`, `ConnectByProxy`), connection strings evaluated with the configuration's values | as they are (package parameters re-serialized when a configuration is applied) | as they are |
| `Project.params` | as stored | re-serialized (root element renamed `ProjectParameters`) | as stored, values set in place |
| Manifest XML declaration | none | none | none (SSISDB rejects one) |
| EncryptSensitiveWithUserKey / password | re-encrypted for the build user | re-encrypted (or `-stripSensitive`) | refused, with what to do instead |

## On the server

Each test project deployed to SQL Server 2019 three times, as built by Visual Studio,
SSISBuild and `onprem-ssis build` (`lab/tools/compare_build.py`), comparing what SSISDB
records (`catalog.packages`, `catalog.object_parameters`):

- **Packages** (names, descriptions, versions, entry points): identical to Visual Studio's
  build in every case, including the one with a package edited outside Visual Studio. There
  SSISBuild's build records the stale description.
- **Parameters**: identical to SSISBuild's build. Visual Studio's build additionally registers
  `CM.<connection>.ConnectByProxy`, `ConnectRetryCount` and `ConnectRetryInterval` for every
  connection manager, because it re-saved the packages with its newer runtime, and with a
  configuration applied it records the evaluated `InitialCatalog` as the connection default.
  At run time the connection expressions are evaluated again, so the packages behave the same.

## Rebuild, then Deploy Package

The everyday Visual Studio loop: change a package, **Rebuild** the project, right-click the
package, **Deploy Package**. Measured on SQL Server 2019 with `lab/tools/compare_package_deploy.py`:

1. Rebuild writes `obj/<Configuration>/` with every package re-saved by Visual Studio's SSIS
   runtime (configuration values applied, newer connection properties added) and
   `bin/<Configuration>/<Project>.ispac` with the same packages.
2. Deploy Package runs the deployment wizard (Visual Studio's own `ISDeploymentWizard.exe`,
   argument `ModelType=Package`, `Packages=...`), which makes one `catalog.deploy_packages`
   call with the selected packages. Project parameters and connection managers are not sent.
3. **The server then re-saves every package in the project** with its own runtime, not just
   the deployed ones: `LastModifiedProductVersion` becomes the server's version, connection
   properties it does not know (`ConnectRetryCount`, `ConnectRetryInterval`, `ConnectByProxy`)
   are dropped, and every expression-driven value (a connection string built from
   `$Project::TargetDb`) is recalculated from the deployed parameter values.

Because of (3), whether the wizard is given the source `.dtsx` or Rebuild's re-saved copy, the
server ends up with the same package. The lab deployed one changed package four ways (wizard
from the source folder, wizard from `obj/Development`, `onprem-ssis deploy-packages`,
`onprem-ssis deploy-project --dtproj --only`): the stored project was identical each time, and
only the deployed package changed.

So the `onprem-ssis` equivalent of Rebuild + Deploy Package is:

```bash
onprem-ssis deploy-packages --env dev --folder STAGE --project STAGE STAGE/Load_Customer.dtsx
# or, building from the .dtproj first (configuration applied, metadata refreshed):
onprem-ssis deploy-project --env dev --folder STAGE --project STAGE --dtproj STAGE/STAGE.dtproj --only Load_Customer
```

with the plan, overwrite protection and verification Visual Studio does not have.
`onprem-ssis` compares packages by a content hash that ignores what the server rewrites in (3),
so a colleague's real change is still caught but the server's own re-save of the other
packages is not mistaken for one.

### Observed in the Visual Studio UI

`lab.py verify` (check `visual-studio-ui`) drives Visual Studio 2022 itself in a signed-in desktop
session: open the solution, **Build > Rebuild Solution**, right-click `Load_B.dtsx` in Solution
Explorer, **Deploy Package**, fill in the wizard, **Deploy**, with an Extended Events capture on the
server. What it shows, every run:

- The wizard skips its source page; the Review page shows the command line Visual Studio built:
  `/Silent /ModelType:Package /SourcePath:"<the project folder>" /Packages:"Load_B.dtsx;"`.
  **The package comes from the project folder, the `.dtsx` you edited, not from Rebuild's `obj/`
  or `bin/` output.** Rebuild does not change what Deploy Package sends.
- The wizard reports Loading packages, Connecting, Changing packages protection level, Deploying
  packages; on the server that is one `catalog.deploy_packages` call with that one package.
- Only `Load_B` changes on the server, it equals the edited source, and the project gets one new
  version. `Load_A` and every other package stay as they were, as they do for
  `onprem-ssis deploy-packages`.

## Deploying like Visual Studio

| Visual Studio action | What it does on the server | `onprem-ssis` |
|---|---|---|
| Project > Deploy | `catalog.deploy_project` with the whole build: every package replaced, packages only on the server removed, no check for others' changes ([deploy-comparison.md](deploy-comparison.md)) | `deploy-project --all`: every package in the build, each checked for others' changes (`--overwrite` to replace one knowingly); packages only on the server are kept |
| Deploy Package (selected packages) | the deployment wizard in package mode with the project folder as source: one `catalog.deploy_packages` call with those packages (observed in the UI, above) | `deploy-packages` or `deploy-project --only`: `catalog.deploy_packages` with those packages, after a plan with the overwrite checks |

## Limits

- Project deployment model only (`.dtproj` with `<DeploymentModel>Project</DeploymentModel>`).
- Only `Project::` configuration values are applied; others are listed in `warnings`.
- The manifest's connection-manager parameters (`CM.*`) are kept as the `.dtproj` has them,
  not regenerated, so a connection manager added outside Visual Studio needs one save in
  Visual Studio.
