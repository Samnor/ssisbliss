# On-demand SQL Server lab on AWS

Goal: a Windows + SQL Server environment that can be created and destroyed at will,
matching the versions we meet in the field, with mock databases, an SSIS catalog and
SQL Agent jobs that the tools in this repo can be built and tested against.

## Target to match (first reference environment)

| Item | Value |
|---|---|
| Engine | SQL Server 2019, build **15.0.4470.1** (CU32 line + later security update) |
| Topology | Two named instances on separate hosts (a test and a prod-like box) |
| Auth | Windows (domain) authentication |
| Features in use | Database Engine, SQL Agent, **SSIS catalog (SSISDB)**, **CDC**, **Master Data Services (MDS)**, **SSAS** (jobs process cubes/models), PolyBase folder present |
| Edition | Unknown yet. MDS implies Enterprise or Developer. Confirm with the query below. |

Run this on the real servers to complete the picture (output is safe to share, no data):

```sql
SELECT SERVERPROPERTY('ProductVersion')      AS version,
       SERVERPROPERTY('ProductUpdateLevel')  AS cu,
       SERVERPROPERTY('ProductUpdateReference') AS kb,
       SERVERPROPERTY('Edition')             AS edition,
       SERVERPROPERTY('Collation')           AS collation,
       @@VERSION                             AS full_version;
-- Windows version, SSIS catalog version, SSAS mode (multidimensional / tabular) also useful.
```

## Mock data platform shape (company-neutral)

A medallion warehouse (bronze, silver, gold layers), with generic names:

- Databases: `BRONZE_API` -> `BRONZE` -> `SILVER` -> `GOLD` (core warehouse) -> several `GOLD_*` data marts,
  `OUTBOUND`, `MDS`, `CONTROL`, `AUDIT`, `SSISDB`
- Read-only history databases (`GOLD_ARCHIVE`, `GOLD_..._ARCHIVE`)
- CDC enabled on `GOLD` and one data mart, with the `cdc.*_capture` / `cdc.*_cleanup` jobs
- SSISDB: one folder per layer, each with a project, packages and an **environment** bound to it
- SQL Agent: numbered chain jobs (`1 BRONZE_API` ... `8 GOLD_...`), event-triggered variants,
  SSAS processing jobs, a "load in progress" guard job, some disabled/failing jobs
- Volumes: small synthetic data, enough rows to make execution history and stats realistic

All names and data are made up; nothing comes from a real system.

## AWS design

- Account/profile and region: your own, in the gitignored `lab/lab.local.json` (see `lab.py`).
- **Edition: SQL Server 2019 Developer** (free, full Enterprise feature set incl. MDS, SSAS, CDC),
  installed on a plain Windows Server 2022 AMI. Avoids the license-included SQL AMI cost.
  Patched to the exact CU + GDR that gives 15.0.4470.1.
- **Instance**: `m6i.xlarge` (4 vCPU / 16 GB) by default, 150 GB gp3. Two named instances on one VM
  (`LAB\TEST`, `LAB\PROD`-style) is cheaper than two VMs; a second VM is an option if we need to
  mimic cross-server linked servers / SSIS across hosts.
- **Access with no open inbound ports**: SSM Session Manager.
  - Claude runs PowerShell on the box with `aws ssm send-command` / `start-session`.
  - Port forwarding `localhost:11433 -> 1433` lets `sqlcmd` / Python on the Mac connect.
  - Port forwarding `localhost:13389 -> 3389` for RDP when you want SSMS/Visual Studio by hand.
- **Windows auth**: optional phase 2 - promote a small AD DS (`lab.local`) so Windows-auth
  behaviour, proxies and SSIS credentials can be mocked. Phase 1 uses SQL auth + local Windows accounts.
- **IaC**: Terraform (`lab/terraform`): VPC/subnet (or default VPC), security group with no inbound,
  IAM instance role with `AmazonSSMManagedInstanceCore`, S3 bucket for installers/backups, the EC2 instance.
  Every resource tagged `project=ssisbliss-lab` so cleanup is one filter.

## Lifecycle: build once, launch many

1. `lab build-image` (slow, ~1-2 h, rarely): launch base Windows, download SQL 2019 Developer media,
   install engine + Agent + SSIS + SSAS + MDS + PolyBase, apply the CU/GDR, install SSMS and
   SSIS/SSDT tooling, then snapshot to a **golden AMI** (`ssisbliss-lab-2019-4470`).
   One AMI per target build; more versions (2016/2017/2022) added later the same way.
2. `lab up [--version 2019-4470]` (~10 min): launch from the golden AMI, run the mock-seed scripts
   (databases, CDC, SSISDB deploy, Agent jobs).
3. `lab stop` / `lab start`: keep the box, pay only for disk while stopped.
4. `lab down`: terminate everything; nothing left running.
5. Optional: auto-stop after N hours idle (EventBridge schedule or an in-guest scheduled task)
   so a forgotten lab doesn't run for days.

## Rough cost (a European region, Windows license included in EC2 price; varies by region)

- m6i.xlarge Windows: roughly USD 0.35/h while running.
- 150 GB gp3: roughly USD 12/month while the instance or AMI snapshot exists.
- Golden AMI snapshots: a few USD/month per version.
Check current prices before relying on these.

## Repo layout this adds

```
lab/
  PLAN.md            this file
  terraform/         network, IAM, instance
  image/             PowerShell: install SQL, apply CU, install tools (run via SSM)
  seed/              T-SQL + SSIS projects that create the mock platform
  lab.py             up / down / stop / start / tunnel / status
```

## Open questions

1. Edition, Windows Server version and collation of the real servers (query above).
2. Other versions to cover besides 2019 (e.g. 2016, 2017, 2022)?
3. SSAS: multidimensional or tabular?
4. Is the AD domain (phase 2) needed early, or is SQL auth fine to start?
