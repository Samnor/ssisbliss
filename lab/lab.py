#!/usr/bin/env python3
"""On-demand SQL Server lab on AWS.

Wraps Terraform (lab/terraform) and SSM so the lab can be created, used and
destroyed from the command line, by a person or an agent. Only needs the AWS CLI,
the Session Manager plugin and Terraform on PATH.

Settings come from environment variables or a gitignored lab/lab.local.json:
    {"aws_profile": "...", "region": "<your AWS region>", "name": "ssisbliss-lab"}
(or LAB_AWS_PROFILE / AWS_PROFILE and LAB_REGION / AWS_REGION).

Images per version (versions.json): "golden" = SQL installed and patched,
"seeded" = golden + SSISDB and the mock estate (seed/estate.json). `up` prefers seeded.

Commands:
    up [--stage S | --base]     launch from the newest saved image, run boot/ scripts, health check
    build-image                 install SQL from scratch and save a golden image
    seed [--save]               create SSISDB + mock estate; --save stores a seeded image
    save-image --stage S        save the running host as an image
    install-ide                 Visual Studio 2022 + SSIS Projects, SSIS DevOps tools, RDP login labadmin
    verify                      every real-server check (deploys, builds, Agent, the Visual Studio UI), PASS/FAIL
    install-tools               build this repo's wheel and install onprem-ssis on the host
                                (then: run tools/smoke-ssis.ps1 for an end-to-end SSISDB test)
    run SCRIPT.ps1|.py [...]    run PowerShell (or Python, with the install-tools venv) on the host via SSM
    ps "COMMAND"                run an inline PowerShell command on the host
    sql "QUERY" [--instance N]  run T-SQL on the host (Windows auth as SYSTEM)
    tunnel [--instance N|--rdp] forward a host port to localhost
    status | stop | start | down
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

LAB_DIR = Path(__file__).resolve().parent
TF_DIR = LAB_DIR / "terraform"
IMAGE_DIR = LAB_DIR / "image"
BOOT_DIR = LAB_DIR / "boot"
SEED_DIR = LAB_DIR / "seed"
TOOLS_DIR = LAB_DIR / "tools"
VERSIONS = json.loads((LAB_DIR / "versions.json").read_text())
def settings() -> dict:
    local = LAB_DIR / "lab.local.json"
    cfg = json.loads(local.read_text()) if local.exists() else {}
    return {
        "aws_profile": os.environ.get("LAB_AWS_PROFILE") or cfg.get("aws_profile") or os.environ.get("AWS_PROFILE", ""),
        "name": os.environ.get("LAB_NAME") or cfg.get("name") or "ssisbliss-lab",
        "region": (os.environ.get("LAB_REGION") or cfg.get("region") or os.environ.get("AWS_REGION")
                   or os.environ.get("AWS_DEFAULT_REGION") or ""),
    }


CFG = settings()
# Prefix of every AWS resource, tag and parameter; a lab created under another name keeps it (lab.local.json "name").
NAME = CFG["name"]
# Secrets are managed here, not by Terraform, so they outlive `down` and match saved images.
SA_PARAM = f"/{NAME}/sa_password"
CATALOG_PARAM = f"/{NAME}/ssisdb_password"
RDP_PARAM = f"/{NAME}/rdp_password"   # local login labadmin, created by install-ide


def log(msg: str) -> None:
    print(f"[lab] {msg}", file=sys.stderr, flush=True)


def aws(*args: str, parse: bool = True) -> Any:
    cmd = ["aws", *args, "--region", CFG["region"], "--cli-read-timeout", "60"]
    if CFG["aws_profile"]:
        cmd += ["--profile", CFG["aws_profile"]]
    if parse:
        cmd += ["--output", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"aws {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return json.loads(res.stdout) if parse and res.stdout.strip() else res.stdout


def terraform(*args: str, capture: bool = False) -> str:
    env = {**os.environ, "TF_IN_AUTOMATION": "1"}
    if not (TF_DIR / ".terraform").exists():
        subprocess.run(["terraform", "init", "-input=false"], cwd=TF_DIR, check=True, env=env)
    res = subprocess.run(["terraform", *args], cwd=TF_DIR, env=env, text=True, capture_output=capture)
    if res.returncode != 0:
        sys.exit(f"terraform {args[0]} failed{': ' + res.stderr if capture else ''}")
    return res.stdout if capture else ""


def tf_outputs() -> dict:
    raw = json.loads(terraform("output", "-json", capture=True) or "{}")
    return {k: v["value"] for k, v in raw.items()}


def instance_id() -> str:
    iid = tf_outputs().get("instance_id")
    if not iid:
        sys.exit("No lab host. Run: lab.py up")
    return iid


def find_image(version: str, stage: str | None = None) -> tuple[str, str] | None:
    """Newest saved image for version: (ami, stage). Without stage, seeded beats golden."""
    images = aws("ec2", "describe-images", "--owners", "self",
                 "--filters", f"Name=tag:project,Values={NAME}", f"Name=tag:lab_version,Values={version}",
                 "Name=state,Values=available")["Images"]
    def stage_of(img: dict) -> str:
        return next((t["Value"] for t in img.get("Tags", []) if t["Key"] == "lab_stage"), "golden")
    candidates = [i for i in images if stage in (None, stage_of(i))]
    candidates.sort(key=lambda i: (stage_of(i) == "seeded", i["CreationDate"]), reverse=True)
    return (candidates[0]["ImageId"], stage_of(candidates[0])) if candidates else None


def ensure_secret(name: str) -> None:
    """Create a SecureString password parameter if it does not exist; never overwrite."""
    res = subprocess.run(["aws", "ssm", "get-parameter", "--name", name, "--region", CFG["region"],
                          *(["--profile", CFG["aws_profile"]] if CFG["aws_profile"] else [])],
                         capture_output=True, text=True)
    if res.returncode == 0:
        return
    if "ParameterNotFound" not in res.stderr:
        sys.exit(f"reading {name} failed: {res.stderr.strip()}")
    alphabet = string.ascii_letters + string.digits
    pw = (secrets.choice(string.ascii_uppercase) + secrets.choice(string.ascii_lowercase)
          + secrets.choice(string.digits) + "-" + "".join(secrets.choice(alphabet + "-_=+#%") for _ in range(20)))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(pw)
    try:
        aws("ssm", "put-parameter", "--name", name, "--type", "SecureString", "--value", f"file://{f.name}",
            "--tags", f"Key=project,Value={NAME}")
    finally:
        os.unlink(f.name)
    log(f"created secret {name}")


def migrate_state() -> None:
    """Older state managed the sa password; hand it over to ensure_secret without deleting it."""
    if not (TF_DIR / "terraform.tfstate").exists():
        return
    listed = terraform("state", "list", capture=True).split()
    old = [r for r in ("aws_ssm_parameter.sa_password", "random_password.sa") if r in listed]
    if old:
        terraform("state", "rm", *old, capture=True)
        log(f"stopped managing {', '.join(old)} in Terraform (parameter kept)")


def apply(ami_id: str, version: str) -> None:
    migrate_state()
    ensure_secret(SA_PARAM)
    ensure_secret(CATALOG_PARAM)
    ensure_secret(RDP_PARAM)
    terraform("apply", "-input=false", "-auto-approve",
              f"-var=aws_profile={CFG['aws_profile']}", f"-var=region={CFG['region']}", f"-var=name={NAME}",
              f"-var=ami_id={ami_id}", f"-var=lab_version={version}")


def wait_ssm_online(iid: str, timeout: int = 900) -> None:
    log(f"waiting for SSM agent on {iid}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = aws("ssm", "describe-instance-information",
                   "--filters", f"Key=InstanceIds,Values={iid}")["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            return
        time.sleep(15)
    sys.exit(f"{iid} did not come online in SSM within {timeout}s")


def lab_context(version: str | None) -> dict:
    ctx = dict(VERSIONS[version]) if version else {}
    ctx.update(region=CFG["region"], bucket=tf_outputs().get("bucket"),
               sa_password_parameter=SA_PARAM, catalog_password_parameter=CATALOG_PARAM,
               rdp_password_parameter=RDP_PARAM)
    if version:
        ctx["estate"] = json.loads((SEED_DIR / "estate.json").read_text())
    return ctx


def run_powershell(iid: str, body: str, ctx: dict, timeout_s: int = 7200, label: str = "inline") -> str:
    """Run PowerShell on the host with $Lab bound to ctx. Returns stdout; exits on failure."""
    script = "$Lab = ConvertFrom-Json @'\n" + json.dumps(ctx) + "\n'@\n" + body
    params = {"commands": script.splitlines(), "executionTimeout": [str(timeout_s)]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(params, f)
    try:
        cmd = aws("ssm", "send-command", "--instance-ids", iid, "--document-name", "AWS-RunPowerShellScript",
                  "--comment", f"lab {label}"[:100], "--timeout-seconds", "600",
                  "--parameters", f"file://{f.name}")["Command"]
    finally:
        os.unlink(f.name)
    cid = cmd["CommandId"]
    log(f"{label}: command {cid}")
    while True:
        time.sleep(10)
        res = aws("ssm", "get-command-invocation", "--command-id", cid, "--instance-id", iid)
        if res["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
    out, err = res.get("StandardOutputContent", ""), res.get("StandardErrorContent", "")
    if res["Status"] != "Success":
        sys.exit(f"{label} {res['Status']}:\n{out}\n{err}")
    return out


def run_dir(iid: str, directory: Path, ctx: dict, lib: Path | None = None) -> None:
    prefix = lib.read_text() + "\n" if lib else ""
    for step in sorted(p for p in directory.glob("*.ps1") if p != lib):
        print(run_powershell(iid, prefix + step.read_text(), ctx, label=step.name), flush=True)


def boot(iid: str, version: str, expect_seeded: bool) -> None:
    """Make a freshly launched host match the current lab settings, then health check."""
    ctx = lab_context(version)
    ctx["expect_seeded"] = expect_seeded
    run_dir(iid, BOOT_DIR, ctx)


def save_image(iid: str, version: str, stage: str) -> str:
    name = f"{NAME}-{version}-{stage}-{time.strftime('%Y%m%d-%H%M')}"
    log(f"saving image {name}")
    tags = f"Tags=[{{Key=project,Value={NAME}}},{{Key=lab_version,Value={version}}},{{Key=lab_stage,Value={stage}}}]"
    ami = aws("ec2", "create-image", "--instance-id", iid, "--name", name,
              "--description", f"{VERSIONS[version]['description']} ({stage})",
              "--tag-specifications", f"ResourceType=image,{tags}", f"ResourceType=snapshot,{tags}")["ImageId"]
    wait_image(ami)
    return ami


def wait_image(ami: str, timeout: int = 3600) -> None:
    # `aws ec2 wait image-available` gives up after 10 minutes; large Windows images take longer.
    deadline = time.time() + timeout
    while time.time() < deadline:
        img = aws("ec2", "describe-images", "--image-ids", ami)["Images"][0]
        if img["State"] == "available":
            log(f"image {ami} ready")
            return
        if img["State"] in ("failed", "error", "invalid"):
            sys.exit(f"image {ami} {img['State']}: {img.get('StateReason', {}).get('Message')}")
        time.sleep(30)
    sys.exit(f"image {ami} not available after {timeout}s")


def reboot(iid: str) -> None:
    log("rebooting")
    aws("ec2", "reboot-instances", "--instance-ids", iid, parse=False)
    time.sleep(60)
    wait_ssm_online(iid)


# ---------------------------------------------------------------- commands ---

def cmd_up(a) -> None:
    if a.base:
        apply("", "base")
        wait_ssm_online(instance_id())
    else:
        found = find_image(a.version, a.stage)
        if not found:
            sys.exit(f"No saved image for {a.version}. Build one: lab.py build-image")
        ami, stage = found
        log(f"launching {ami} ({stage})")
        apply(ami, a.version)
        iid = instance_id()
        wait_ssm_online(iid)
        boot(iid, a.version, expect_seeded=stage == "seeded")
    cmd_status(a)


def cmd_seed(a) -> None:
    iid = instance_id()
    ensure_secret(SA_PARAM)
    ensure_secret(CATALOG_PARAM)
    ensure_secret(RDP_PARAM)
    run_dir(iid, SEED_DIR, lab_context(a.version), lib=SEED_DIR / "lib.ps1")
    boot(iid, a.version, expect_seeded=True)
    if a.save:
        print(save_image(iid, a.version, "seeded"))


def cmd_save_image(a) -> None:
    print(save_image(instance_id(), a.version, a.stage))


def cmd_build_image(a) -> None:
    if a.version not in VERSIONS:
        sys.exit(f"Unknown version {a.version}; known: {', '.join(VERSIONS)}")
    apply("", "base")
    iid = instance_id()
    wait_ssm_online(iid)
    ctx = lab_context(a.version)
    steps = sorted(IMAGE_DIR.glob("*.ps1"))
    for step in steps:
        print(run_powershell(iid, step.read_text(), ctx, label=step.name))
        if step.name.startswith(("02-", "03-")):
            reboot(iid)
    run_dir(iid, BOOT_DIR, {**ctx, "expect_seeded": False})
    print(save_image(iid, a.version, "golden"))
    log("golden image ready. Next: lab.py seed --save, or launch it with: lab.py up")


def install_tools(iid: str) -> None:
    """Build this repo's wheel, upload it to the lab bucket and install it on the host."""
    dist = Path(tempfile.mkdtemp())
    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(dist)], cwd=LAB_DIR.parent, check=True)
    wheel = next(dist.glob("*.whl"))
    ctx = lab_context(None)
    key = f"tools/{wheel.name}"
    aws("s3", "cp", str(wheel), f"s3://{ctx['bucket']}/{key}", parse=False)
    print(run_powershell(iid, (TOOLS_DIR / "install-ssisbliss.ps1").read_text(),
                         {**ctx, "wheel_key": key}, label="install-tools"))


def cmd_install_tools(a) -> None:
    install_tools(instance_id())


def cmd_install_ide(a) -> None:
    """Visual Studio 2022 + SSIS Projects, SSIS DevOps tools, and an RDP login (labadmin)."""
    ensure_secret(RDP_PARAM)
    iid = instance_id()
    ctx = lab_context(a.version)
    print(run_powershell(iid, (TOOLS_DIR / "install-ide.ps1").read_text(), ctx, timeout_s=10800, label="install-ide"))
    print(run_powershell(iid, (BOOT_DIR / "30-rdp-user.ps1").read_text(), ctx, label="30-rdp-user"))
    log(f"RDP: lab.py tunnel --rdp, then connect to localhost:13389 as labadmin; password: aws ssm get-parameter "
        f"--name {RDP_PARAM} --with-decryption --query Parameter.Value --output text")


def run_python(iid: str, path: Path, ctx: dict, args: str = "", timeout_s: int = 7200) -> str:
    """Run a Python script on the host with the install-tools venv; $Lab is passed as LAB_JSON."""
    body = path.read_text()
    if "\n'@" in body:
        sys.exit(f"{path} contains a line starting with '@ and cannot be embedded")
    script = ("$env:LAB_JSON = $Lab | ConvertTo-Json -Depth 10 -Compress\n"
              f"@'\n{body}\n'@ | Set-Content -Encoding utf8 C:\\lab\\work\\{path.name}\n"
              f"& C:\\lab\\tools\\venv\\Scripts\\python.exe C:\\lab\\work\\{path.name} {args}\n"
              "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }")
    return run_powershell(iid, script, ctx, timeout_s=timeout_s, label=path.name)


def ensure_desktop_session(iid: str, ctx: dict) -> None:
    """labadmin signed in at the console (automatic sign-in, lab only), for UI automation."""
    if "labadmin" in run_powershell(iid, "quser 2>&1 | Out-String", {}, label="sessions"):
        return
    print(run_powershell(iid, (TOOLS_DIR / "vs-ui" / "autologon.ps1").read_text(), ctx, label="autologon"))
    reboot(iid)
    time.sleep(60)


def run_in_desktop(iid: str, ctx: dict, body: str, out: Path, timeout: int = 900) -> list[str]:
    """Run PowerShell in labadmin's desktop session (an interactive scheduled task) and fetch its $Out
    folder (log, screenshots, results) into `out`."""
    if "\n'@" in body:
        sys.exit("desktop script contains a line starting with '@ and cannot be embedded")
    wrapper = f"""
$ErrorActionPreference = 'Stop'
$base = 'C:\\lab\\work\\vsui'; $out = "$base\\out"
New-Item -ItemType Directory -Force $base | Out-Null
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Force $out | Out-Null
icacls $base /grant "Users:(OI)(CI)M" | Out-Null
@'
$Out = 'C:\\lab\\work\\vsui\\out'
try {{
{body}
}} catch {{ "SCRIPT ERROR: $_`n$($_.ScriptStackTrace)" | Add-Content "$Out\\log.txt" }}
finally {{ 'done' | Set-Content "$Out\\done.txt" }}
'@ | Set-Content -Encoding utf8 "$base\\ui.ps1"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -STA -File $base\\ui.ps1"
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\\labadmin" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName onprem-vsui -Action $action -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName onprem-vsui
$deadline = (Get-Date).AddSeconds({timeout})
while ((Get-Date) -lt $deadline -and -not (Test-Path "$out\\done.txt")) {{ Start-Sleep 5 }}
if (-not (Test-Path "$out\\done.txt")) {{ 'TIMEOUT' | Add-Content "$out\\log.txt"; Stop-ScheduledTask -TaskName onprem-vsui }}
Unregister-ScheduledTask -TaskName onprem-vsui -Confirm:$false
Add-Type -AssemblyName System.IO.Compression.FileSystem
Remove-Item "$base\\out.zip" -ErrorAction SilentlyContinue
[IO.Compression.ZipFile]::CreateFromDirectory($out, "$base\\out.zip")
Write-S3Object -BucketName $Lab.bucket -Region $Lab.region -Key debug/vsui-out.zip -File "$base\\out.zip"
"""
    run_powershell(iid, wrapper, ctx, timeout_s=timeout + 300, label="desktop")
    out.mkdir(parents=True, exist_ok=True)
    local = out / "out.zip"
    aws("s3", "cp", f"s3://{ctx['bucket']}/debug/vsui-out.zip", str(local), parse=False)
    import zipfile
    with zipfile.ZipFile(local) as z:
        for i in z.infolist():
            dest = out / i.filename.replace("\\", "/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(i))
    local.unlink()
    return sorted(p.name for p in out.iterdir())


def cmd_verify(a) -> None:
    import verify
    out = LAB_DIR / "verify-runs" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    ok = verify.run(sys.modules[__name__], instance_id(), a.version, out, a.only)
    sys.exit(0 if ok else 1)


def cmd_run(a) -> None:
    iid = instance_id()
    for path in a.scripts:
        if path.endswith(".py"):
            print(run_python(iid, Path(path), lab_context(a.version), a.args, timeout_s=a.timeout))
        else:
            print(run_powershell(iid, Path(path).read_text(), lab_context(a.version), timeout_s=a.timeout,
                                 label=Path(path).name))


def cmd_ps(a) -> None:
    print(run_powershell(instance_id(), a.command, lab_context(a.version), timeout_s=a.timeout))


def cmd_sql(a) -> None:
    port = instance_port(a.version, a.instance)
    query = a.query.replace("'", "''")
    body = (
        "$sqlcmd = Get-ChildItem 'C:\\Program Files\\Microsoft SQL Server\\Client SDK\\ODBC' -Recurse -Filter SQLCMD.EXE"
        " | Select-Object -First 1 -ExpandProperty FullName\n"
        f"& $sqlcmd -S 'localhost,{port}' -E -b -W -s '|' -Q '{query}'\n"
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
    )
    print(run_powershell(instance_id(), body, {}, timeout_s=a.timeout))


def instance_port(version: str, name: str | None) -> int:
    instances = VERSIONS[version]["instances"]
    if name is None:
        return instances[0]["port"]
    for inst in instances:
        if inst["name"].lower() == name.lower():
            return inst["port"]
    sys.exit(f"Unknown instance {name}; known: {', '.join(i['name'] for i in instances)}")


def cmd_tunnel(a) -> None:
    remote = 3389 if a.rdp else instance_port(a.version, a.instance)
    local = a.local_port or (13389 if a.rdp else remote)
    log(f"localhost:{local} -> host:{remote} (Ctrl-C to stop)")
    if a.rdp:
        log(f"log in as labadmin; password: aws ssm get-parameter --name {RDP_PARAM} --with-decryption "
            f"--query Parameter.Value --output text")
    cmd = ["aws", "ssm", "start-session", "--target", instance_id(), "--region", CFG["region"],
           "--document-name", "AWS-StartPortForwardingSession",
           "--parameters", json.dumps({"portNumber": [str(remote)], "localPortNumber": [str(local)]})]
    if CFG["aws_profile"]:
        cmd += ["--profile", CFG["aws_profile"]]
    os.execvp("aws", cmd)


def cmd_status(a) -> None:
    out = tf_outputs()
    if not out.get("instance_id"):
        print(json.dumps({"state": "absent"}))
        return
    inst = aws("ec2", "describe-instances", "--instance-ids", out["instance_id"])["Reservations"][0]["Instances"][0]
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
    print(json.dumps({"instance_id": inst["InstanceId"], "state": inst["State"]["Name"],
                      "type": inst["InstanceType"], "ami": inst["ImageId"],
                      "lab_version": tags.get("lab_version"), "bucket": out.get("bucket")}, indent=2))


def cmd_stop(a) -> None:
    aws("ec2", "stop-instances", "--instance-ids", instance_id(), parse=False)
    log("stopping; disk is kept")


def cmd_start(a) -> None:
    iid = instance_id()
    aws("ec2", "start-instances", "--instance-ids", iid, parse=False)
    wait_ssm_online(iid)


def cmd_down(a) -> None:
    migrate_state()   # never let destroy take the passwords saved images depend on
    terraform("destroy", "-input=false", "-auto-approve",
              f"-var=aws_profile={CFG['aws_profile']}", f"-var=region={CFG['region']}", f"-var=name={NAME}")
    log("lab destroyed (golden AMIs are kept)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", default=next(iter(VERSIONS)), help="key in versions.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("up"); s.add_argument("--base", action="store_true")
    s.add_argument("--stage", choices=["golden", "seeded"]); s.set_defaults(fn=cmd_up)
    sub.add_parser("build-image").set_defaults(fn=cmd_build_image)
    s = sub.add_parser("seed"); s.add_argument("--save", action="store_true"); s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("save-image"); s.add_argument("--stage", choices=["golden", "seeded"], required=True)
    s.set_defaults(fn=cmd_save_image)
    sub.add_parser("install-tools").set_defaults(fn=cmd_install_tools)
    sub.add_parser("install-ide").set_defaults(fn=cmd_install_ide)
    s = sub.add_parser("verify"); s.add_argument("--only", nargs="*", help="run only these checks")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("run"); s.add_argument("scripts", nargs="+")
    s.add_argument("--args", default="", help='arguments for .py scripts, e.g. --args="--observe 10"')
    s.add_argument("--timeout", type=int, default=7200); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("ps"); s.add_argument("command"); s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(fn=cmd_ps)
    s = sub.add_parser("sql"); s.add_argument("query"); s.add_argument("--instance")
    s.add_argument("--timeout", type=int, default=600); s.set_defaults(fn=cmd_sql)
    s = sub.add_parser("tunnel"); s.add_argument("--instance"); s.add_argument("--rdp", action="store_true")
    s.add_argument("--local-port", type=int); s.set_defaults(fn=cmd_tunnel)
    for name, fn in [("status", cmd_status), ("stop", cmd_stop), ("start", cmd_start), ("down", cmd_down)]:
        sub.add_parser(name).set_defaults(fn=fn)

    a = p.parse_args()
    if not CFG["region"]:
        sys.exit("lab: no AWS region; set \"region\" in lab/lab.local.json, or LAB_REGION / AWS_REGION")
    a.fn(a)


if __name__ == "__main__":
    main()
