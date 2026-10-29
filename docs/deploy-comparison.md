# Microsoft's deploy paths vs `onprem-ssis`

What actually happens on the server when a developer deploys a change, measured in the lab
(SQL Server 2019 15.0.4470.1, SSIS Projects extension 2.2 for Visual Studio 2022, SSIS
DevOps tools 1.0) with `lab/tools/compare_deploy.py`.

## Scenario

The everyday team situation on a shared dev/stage server, in catalog folder `CMP`:

1. Project `STAGE` is deployed, with an environment reference and parameter bindings
   (`TargetDb` from the environment, `LoadMode` a literal).
2. The developer pulls `Stage_Product`.
3. A colleague deploys a work-in-progress change to `Stage_Ledger`.
4. The developer builds the project (a Visual Studio-style `.ispac`, from before the
   colleague's change) with only `Stage_Product` edited, and deploys.

Every method starts from the same server state and the same developer base record. SQL
calls are captured with an Extended Events session.

## Result

| Method | Developer's change | Colleague's `Stage_Ledger` | Bindings, environment reference | SSISDB call |
|---|---|---|---|---|
| `ISDeploymentWizard.exe` (what Visual Studio's project **Deploy** runs) | deployed | **overwritten with the stale copy** | kept | `catalog.deploy_project` |
| `SSISDeploy.exe` (standalone SSIS DevOps tools, the CI/CD path) | deployed | **overwritten with the stale copy** | kept | `catalog.deploy_project` |
| `onprem-ssis deploy-project --only Stage_Product` | deployed | kept | kept | `catalog.deploy_packages` |
| `onprem-ssis deploy-packages Stage_Product.dtsx` | deployed | kept | kept | `catalog.deploy_packages` |

Both Microsoft project-deploy paths replace every package with what is in the build, with no
warning: the colleague's deployed work is gone (still restorable from the project's version
history, if anyone notices). Bindings and environment references survive a project redeploy,
so the only damage is to other people's packages, which is exactly what is easy to miss.

`onprem-ssis` uses `catalog.deploy_packages`, so the server keeps every other package byte
for byte, and its base records would have refused to overwrite `Stage_Product` too if a
colleague had changed that one (see [ssis-deploy.md](ssis-deploy.md)).

Behind `catalog.deploy_project` and `catalog.deploy_packages` the server runs the same
internal steps (`internal.append_parameter`, `internal.sync_parameter_versions`, ...); the
difference is only which packages it is given.

## Visual Studio's Deploy Package

Measured through the Visual Studio UI itself (Rebuild, right-click a package, Deploy Package):
one `catalog.deploy_packages` call with the selected package from the project folder; every
other package stays as it was. That is what `onprem-ssis deploy-packages` does too, plus its
checks. Details in [build.md](build.md#observed-in-the-visual-studio-ui).

## Reproduce

```bash
python lab/lab.py install-tools        # onprem-ssis on the host
python lab/lab.py install-ide          # Visual Studio 2022 + SSIS Projects, SSIS DevOps tools, RDP login
python lab/lab.py run lab/tools/compare_deploy.py
python lab/lab.py verify                # this comparison and every other real-server check
```

`--args="--methods wizard,ssisdeploy,onprem-project,onprem-packages"` picks methods. Full
results with every captured statement are in `C:\lab\work\compare\result.json` on the host.
