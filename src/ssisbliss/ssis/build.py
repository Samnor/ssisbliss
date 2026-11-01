"""Build a Visual Studio SSIS project (.dtproj) into an .ispac without Visual Studio.

Matches what Visual Studio's build (devenv) and SSISBuild.exe were measured to do in the lab
(docs/build.md):
  - the manifest comes from the .dtproj, with each package's metadata (version, description,
    protection level, package parameters) refreshed from the .dtsx file, as Visual Studio does
    (SSISBuild.exe leaves stale metadata in place)
  - the chosen configuration's parameter values are written into Project.params
  - packages, connection managers and parameters are otherwise included as they are, like
    SSISBuild.exe (Visual Studio also re-saves packages with its own, newer SSIS runtime)
  - no XML declaration in the manifest; the same [Content_Types].xml
Only DontSaveSensitive projects are built: the other protection levels need the Windows user
key or a password to re-encrypt sensitive values.
"""
from __future__ import annotations

import copy
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .dtsx import DTS, PROTECTION_LEVELS
from .ispac import SSIS

_S, _D = f"{{{SSIS}}}", f"{{{DTS}}}"
CONTENT_TYPES = ('<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                 'content-types"><Default Extension="dtsx" ContentType="text/xml" /><Default Extension="conmgr" '
                 'ContentType="text/xml" /><Default Extension="params" ContentType="text/xml" /><Default '
                 'Extension="manifest" ContentType="text/xml" /></Types>')
# Package parameter DTS:DataType is a COM VARTYPE; the manifest uses .NET TypeCode.
VARTYPE_TO_TYPECODE = {2: 7, 3: 9, 4: 13, 5: 14, 6: 15, 7: 16, 8: 18, 11: 3, 14: 15, 16: 5, 17: 6, 18: 8, 19: 10,
                       20: 11, 21: 12}
_PACKAGE_PROPERTIES = ("ID", "Name", "VersionMajor", "VersionMinor", "VersionBuild", "VersionComments", "VersionGUID",
                       "PackageFormatVersion", "Description", "ProtectionLevel")


class BuildError(ValueError):
    pass


class ProtectionRefused(BuildError):
    """The project or a package is not DontSaveSensitive: only the developer can change that."""


@dataclass
class DtProj:
    path: Path
    manifest: ET.Element
    configurations: dict[str, dict[str, str]]   # configuration -> {"Project::X": value}
    target_server_version: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        prop = self.manifest.find(f"{_S}Properties/{_S}Property[@{_S}Name='Name']")
        return (prop.text or "").strip() if prop is not None and prop.text else self.path.stem


def load_dtproj(path: Path) -> DtProj:
    root = ET.fromstring(path.read_bytes())
    if root.findtext("DeploymentModel", "Project") != "Project":
        raise BuildError(f"{path.name} uses the package deployment model; only project deployment projects build "
                         f"to an .ispac")
    project = root.find("DeploymentModelSpecificContent/Manifest/" + f"{_S}Project")
    if project is None:
        raise BuildError(f"{path.name} has no project manifest (DeploymentModelSpecificContent/Manifest is empty); "
                         f"open and save it once in Visual Studio")
    configs, versions = {}, {}
    for c in root.findall("Configurations/Configuration"):
        name = c.findtext("Name", "")
        configs[name] = {s.findtext("Name", ""): s.findtext("Value", "") for s in
                         c.findall("Options/ParameterConfigurationValues/ConfigurationSetting")}
        versions[name] = c.findtext("Options/TargetServerVersion", "")
    return DtProj(path, project, configs, versions)


def build(dtproj: Path, configuration: str | None = None) -> tuple[bytes, dict]:
    proj = load_dtproj(dtproj)
    folder = dtproj.parent
    if configuration is None:
        configuration = "Development" if "Development" in proj.configurations or not proj.configurations \
            else next(iter(proj.configurations))
    elif proj.configurations and configuration not in proj.configurations:
        raise BuildError(f"no configuration '{configuration}' in {dtproj.name}; it has {', '.join(proj.configurations)}")
    manifest = copy.deepcopy(proj.manifest)
    report: dict = {"project": proj.name, "dtproj": str(dtproj), "configuration": configuration,
                    "target_server_version": proj.target_server_version.get(configuration) or None,
                    "packages": [], "refreshed_metadata": {}, "applied_configuration": {}, "warnings": []}

    level = manifest.get(f"{_S}ProtectionLevel", "DontSaveSensitive")
    if level != "DontSaveSensitive":
        raise ProtectionRefused(f"the project's protection level is {level}. Building it means re-encrypting sensitive "
                                f"values with your Windows user key or a password, which only Visual Studio or "
                                f"SSISBuild.exe can do, and the catalog cannot use user-key encryption anyway. Set the "
                                f"project and its packages to DontSaveSensitive and keep sensitive values in catalog "
                                f"environments, or build with SSISBuild.exe -stripSensitive")

    packages = [p.get(f"{_S}Name", "") for p in manifest.findall(f"{_S}Packages/{_S}Package")]
    conmgrs = [c.get(f"{_S}Name", "") for c in manifest.findall(f"{_S}ConnectionManagers/{_S}ConnectionManager")]
    missing = [n for n in packages + conmgrs if not (folder / n).is_file()]
    if missing:
        raise BuildError(f"files listed in {dtproj.name} are missing from {folder}: {', '.join(missing)}")
    extra = sorted(p.name for p in folder.glob("*.dtsx") if p.name not in packages and not p.name.endswith(".server.dtsx"))
    if extra:
        report["warnings"].append(f"not part of the project, not built: {', '.join(extra)}")

    parts: list[tuple[str, bytes]] = []
    info = manifest.find(f"{_S}DeploymentInfo/{_S}PackageInfo")
    for name in packages:
        data = (folder / name).read_bytes()
        changes = _refresh_package_metadata(info, name, data)
        if changes:
            report["refreshed_metadata"][name] = changes
        parts.append((name, data))
        report["packages"].append(name)
    for name in conmgrs:
        parts.append((name, (folder / name).read_bytes()))
    params_file = folder / "Project.params"
    if params_file.is_file():
        params, applied, unknown = _apply_configuration(params_file.read_bytes(), proj.configurations.get(configuration, {}))
        report["applied_configuration"] = applied
        if unknown:
            report["warnings"].append(f"configuration '{configuration}' sets values this build does not apply: "
                                      f"{', '.join(unknown)}")
        parts.append(("Project.params", params))
    # No XML declaration: SSISDB rejects a manifest that has one (see docs/ssis-deploy.md).
    parts.append(("@Project.manifest", ET.tostring(manifest, encoding="utf-8", xml_declaration=False)))
    parts.append(("[Content_Types].xml", b"\xef\xbb\xbf" + CONTENT_TYPES.encode()))

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts:
            z.writestr(name if name.startswith(("@", "[")) else quote(name), data)
    return out.getvalue(), report


def _package_values(data: bytes) -> tuple[dict[str, str], list[dict]]:
    root = ET.fromstring(data)
    a = lambda n, default="": root.get(f"{_D}{n}", default)  # noqa: E731
    fmt = next((p.text or "" for p in root.findall(f"{_D}Property") if p.get(f"{_D}Name") == "PackageFormatVersion"), "")
    props = {"ID": a("DTSID"), "Name": a("ObjectName"), "VersionMajor": a("VersionMajor", "1"),
             "VersionMinor": a("VersionMinor", "0"), "VersionBuild": a("VersionBuild", "0"),
             "VersionComments": a("VersionComments"), "VersionGUID": a("VersionGUID"), "PackageFormatVersion": fmt,
             "Description": a("Description"), "ProtectionLevel": a("ProtectionLevel", "1")}
    params = []
    for p in root.findall(f"{_D}PackageParameters/{_D}PackageParameter"):
        value = next((v.text or "" for v in p.findall(f"{_D}Property") if v.get(f"{_D}Name") == "ParameterValue"), "")
        vartype = int(p.get(f"{_D}DataType", "8"))
        params.append({"Name": p.get(f"{_D}ObjectName", ""), "ID": p.get(f"{_D}DTSID", ""),
                       "CreationName": p.get(f"{_D}CreationName", ""), "Description": p.get(f"{_D}Description", ""),
                       "IncludeInDebugDump": p.get(f"{_D}IncludeInDebugDump", "0"),
                       "Required": "1" if p.get(f"{_D}Required") in ("True", "1") else "0",
                       "Sensitive": "1" if p.get(f"{_D}Sensitive") in ("True", "1") else "0", "Value": value,
                       "DataType": str(VARTYPE_TO_TYPECODE.get(vartype, 18))})
    return props, params


def _refresh_package_metadata(info: ET.Element | None, name: str, data: bytes) -> list[str]:
    """Bring the manifest's PackageMetaData for `name` in line with the .dtsx, as Visual Studio does."""
    if info is None:
        return []
    props, params = _package_values(data)
    if props["ProtectionLevel"] not in ("0",):
        level = PROTECTION_LEVELS.get(int(props["ProtectionLevel"]), props["ProtectionLevel"])
        raise ProtectionRefused(f"{name} has protection level {level}; set it to DontSaveSensitive like the project")
    meta = next((m for m in info.findall(f"{_S}PackageMetaData") if m.get(f"{_S}Name") == name), None)
    changes: list[str] = []
    if meta is None:
        meta = ET.SubElement(info, f"{_S}PackageMetaData", {f"{_S}Name": name})
        ET.SubElement(meta, f"{_S}Properties")
        ET.SubElement(meta, f"{_S}Parameters")
        changes.append("added: the package had no metadata in the .dtproj")
    properties = meta.find(f"{_S}Properties")
    if properties is None:
        properties = ET.SubElement(meta, f"{_S}Properties")
    for key in _PACKAGE_PROPERTIES:
        el = properties.find(f"{_S}Property[@{_S}Name='{key}']")
        if el is None:
            el = ET.SubElement(properties, f"{_S}Property", {f"{_S}Name": key})
        old = (el.text or "").strip()
        if old != props[key]:
            if key != "VersionGUID":   # changes on every save; not worth reporting
                changes.append(f"{key}: {old!r} -> {props[key]!r}")
            el.text = props[key]

    parameters = meta.find(f"{_S}Parameters")
    if parameters is None:
        parameters = ET.SubElement(meta, f"{_S}Parameters")
    # Package parameters come from the .dtsx; CM.<connection>.<property> entries stay as Visual Studio wrote them.
    existing = {p.get(f"{_S}Name"): p for p in parameters.findall(f"{_S}Parameter")}
    wanted = {p["Name"] for p in params}
    for pname, el in existing.items():
        if pname and not pname.startswith("CM.") and pname not in wanted:
            parameters.remove(el)
            changes.append(f"parameter {pname}: removed (not in the package)")
    for i, p in enumerate(params):
        el = existing.get(p["Name"])
        if el is None:
            el = ET.Element(f"{_S}Parameter", {f"{_S}Name": p["Name"]})
            ET.SubElement(el, f"{_S}Properties")
            parameters.insert(i, el)
            changes.append(f"parameter {p['Name']}: added")
        props_el = el.find(f"{_S}Properties")
        if props_el is None:
            props_el = ET.SubElement(el, f"{_S}Properties")
        for key in ("ID", "CreationName", "Description", "IncludeInDebugDump", "Required", "Sensitive", "Value", "DataType"):
            prop = props_el.find(f"{_S}Property[@{_S}Name='{key}']")
            if prop is None:
                prop = ET.SubElement(props_el, f"{_S}Property", {f"{_S}Name": key})
            if (prop.text or "").strip() != p[key]:
                if p["Name"] in existing:
                    changes.append(f"parameter {p['Name']}.{key}: {(prop.text or '').strip()!r} -> {p[key]!r}")
                prop.text = p[key]
    return changes


def _apply_configuration(params: bytes, values: dict[str, str]) -> tuple[bytes, dict[str, str], list[str]]:
    """Set Project::X values in Project.params, changing only the value text (as Visual Studio does)."""
    text = params.decode("utf-8-sig")
    bom = params.startswith(b"\xef\xbb\xbf")
    applied, unknown = {}, []
    for key, value in values.items():
        scope, _, pname = key.partition("::")
        if scope != "Project":
            unknown.append(key)
            continue
        pattern = re.compile(r'(SSIS:Name="' + re.escape(pname) + r'">.*?SSIS:Name="Value"[^>]*>)(.*?)(</SSIS:Property>)',
                             re.S)
        if not pattern.search(text):
            unknown.append(key)
            continue
        escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = pattern.sub(lambda m: m.group(1) + escaped + m.group(3), text, count=1)
        applied[pname] = value
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"), applied, unknown
