"""Read and merge .ispac files (SSIS project deployment archives).

An .ispac is an OPC zip: @Project.manifest, [Content_Types].xml, Project.params,
*.conmgr and *.dtsx. Part names are percent-encoded inside the zip.
"""
from __future__ import annotations

import copy
import io
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import unquote

from .dtsx import DTS, PROTECTION_LEVELS, package_sha256, sha256

SSIS = "www.microsoft.com/SqlServer/SSIS"
_NS = {"SSIS": SSIS, "DTS": DTS}
MANIFEST = "@Project.manifest"
CONTENT_TYPES = "[Content_Types].xml"

ET.register_namespace("SSIS", SSIS)
ET.register_namespace("DTS", DTS)
# OPC readers expect the content types namespace as the default namespace.
ET.register_namespace("", "http://schemas.openxmlformats.org/package/2006/content-types")


def _q(name: str) -> str:
    return f"{{{SSIS}}}{name}"


class Ispac:
    def __init__(self, data: bytes):
        self.data = data
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            # logical name -> (zip entry name, bytes)
            self.parts = {unquote(i.filename): (i.filename, z.read(i)) for i in z.infolist() if not i.is_dir()}
        if MANIFEST not in self.parts:
            raise ValueError("not an .ispac: no @Project.manifest")
        self.manifest = ET.fromstring(self.parts[MANIFEST][1])

    @classmethod
    def from_file(cls, path) -> "Ispac":
        with open(path, "rb") as f:
            return cls(f.read())

    # ------------------------------------------------------------ reading --

    @property
    def protection_level(self) -> int:
        name = self.manifest.get(_q("ProtectionLevel"), "DontSaveSensitive")
        by_name = {v: k for k, v in PROTECTION_LEVELS.items()}
        return by_name.get(name, int(name) if name.isdigit() else -1)

    @property
    def package_names(self) -> list[str]:
        return [p.get(_q("Name"), "") for p in self.manifest.findall("SSIS:Packages/SSIS:Package", _NS)]

    def package(self, name: str) -> bytes:
        return self.parts[name][1]

    def package_hashes(self) -> dict[str, str]:
        return {n: package_sha256(self.package(n)) for n in self.package_names}

    @property
    def connection_manager_files(self) -> list[str]:
        return [c.get(_q("Name"), "") for c in self.manifest.findall("SSIS:ConnectionManagers/SSIS:ConnectionManager", _NS)]

    def connection_manager_names(self) -> set[str]:
        """Object names of project connection managers (what packages reference)."""
        names = set()
        for f in self.connection_manager_files:
            root = ET.fromstring(self.parts[f][1])
            names.add(root.get(f"{{{DTS}}}ObjectName") or f.removesuffix(".conmgr"))
        return names

    def parameter_names(self) -> set[str]:
        if "Project.params" not in self.parts:
            return set()
        root = ET.fromstring(self.parts["Project.params"][1])
        return {p.get(_q("Name"), "") for p in root.findall("SSIS:Parameter", _NS)}

    def project_items(self) -> dict[str, str]:
        """Hash of each project-level file (parameters and connection managers)."""
        names = ["Project.params", *self.connection_manager_files]
        return {n: sha256(self.parts[n][1]) for n in names if n in self.parts}

    def _param_elements(self) -> dict[str, ET.Element]:
        if "Project.params" not in self.parts:
            return {}
        root = ET.fromstring(self.parts["Project.params"][1])
        return {p.get(_q("Name"), ""): p for p in root.findall("SSIS:Parameter", _NS)}

    def item_hashes(self) -> dict[str, str]:
        """Each project parameter ("param:Name") and connection manager ("conmgr:File.conmgr"), hashed so
        that the catalog's re-save of a connection manager does not count as a change."""
        out = {f"param:{n}": sha256(ET.canonicalize(ET.tostring(e, encoding="unicode")).encode())
               for n, e in self._param_elements().items()}
        for f in self.connection_manager_files:
            if f in self.parts:
                out[f"conmgr:{f}"] = package_sha256(self.parts[f][1])
        return out

    def resolve_item(self, name: str) -> str | None:
        """`TargetDb` -> "param:TargetDb"; `Metadata` or `Metadata.conmgr` -> "conmgr:Metadata.conmgr"."""
        if name in self._param_elements():
            return f"param:{name}"
        for f in self.connection_manager_files:
            if name in (f, f.removesuffix(".conmgr")):
                return f"conmgr:{f}"
        return None

    def merged_items(self, source: "Ispac", packages: list[str], items: list[str]) -> bytes:
        """This project with `packages` and the project items `items` (from resolve_item) taken from
        `source`; every other package, parameter and connection manager stays as it is here."""
        base = Ispac(self.merged(source, packages, project_items=False))
        manifest = copy.deepcopy(base.manifest)
        parts = dict(base.parts)
        params = [i.removeprefix("param:") for i in items if i.startswith("param:")]
        if params:
            if "Project.params" in parts:
                root = ET.fromstring(parts["Project.params"][1])
            else:
                root = ET.fromstring(f'<SSIS:Parameters xmlns:SSIS="{SSIS}" />')
            mine = {p.get(_q("Name")): p for p in root.findall("SSIS:Parameter", _NS)}
            theirs = source._param_elements()
            for n in params:
                if n in mine:
                    idx = list(root).index(mine[n])
                    root.remove(mine[n])
                    root.insert(idx, copy.deepcopy(theirs[n]))
                else:
                    root.append(copy.deepcopy(theirs[n]))
            entry = parts.get("Project.params", (source.parts.get("Project.params", ("Project.params", b""))[0], b""))[0]
            parts["Project.params"] = (entry, ET.tostring(root, encoding="utf-8", xml_declaration=True))
        for f in (i.removeprefix("conmgr:") for i in items if i.startswith("conmgr:")):
            parts[f] = source.parts[f]
            cms = manifest.find("SSIS:ConnectionManagers", _NS)
            if cms is None:
                cms = ET.SubElement(manifest, _q("ConnectionManagers"))
            if not any(c.get(_q("Name")) == f for c in cms):
                cms.append(copy.deepcopy(next(c for c in source.manifest.findall("SSIS:ConnectionManagers/SSIS:ConnectionManager", _NS)
                                              if c.get(_q("Name")) == f)))
            # The connection manager's own catalog parameters (CM.<object name>.<property>).
            obj = ET.fromstring(source.parts[f][1]).get(f"{{{DTS}}}ObjectName") or f.removesuffix(".conmgr")
            prefix = f"CM.{obj}."
            target = manifest.find("SSIS:DeploymentInfo/SSIS:ProjectConnectionParameters", _NS)
            src_section = source.manifest.find("SSIS:DeploymentInfo/SSIS:ProjectConnectionParameters", _NS)
            if target is not None and src_section is not None:
                for e in [e for e in target if (e.get(_q("Name")) or "").startswith(prefix)]:
                    target.remove(e)
                for e in src_section:
                    if (e.get(_q("Name")) or "").startswith(prefix):
                        target.append(copy.deepcopy(e))
        # No XML declaration in the manifest: SSISDB 15.0 rejects one.
        parts[MANIFEST] = (base.parts[MANIFEST][0], ET.tostring(manifest, encoding="utf-8", xml_declaration=False))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for zip_name, data in parts.values():
                z.writestr(zip_name, data)
        return out.getvalue()

    # ------------------------------------------------------------ merging --

    def merged(self, source: "Ispac", packages: list[str], project_items: bool) -> bytes:
        """This project with `packages` (and optionally project items) taken from `source`."""
        missing = [p for p in packages if p not in source.package_names]
        if missing:
            raise ValueError(f"not in source .ispac: {', '.join(missing)}")

        manifest = copy.deepcopy(self.manifest)
        parts = dict(self.parts)

        pkgs_el = manifest.find("SSIS:Packages", _NS)
        info_el = manifest.find("SSIS:DeploymentInfo/SSIS:PackageInfo", _NS)
        for name in packages:
            for parent, path in ((pkgs_el, "SSIS:Packages/SSIS:Package"),
                                 (info_el, "SSIS:DeploymentInfo/SSIS:PackageInfo/SSIS:PackageMetaData")):
                if parent is None:
                    continue
                src = next((e for e in source.manifest.findall(path, _NS) if e.get(_q("Name")) == name), None)
                old = next((e for e in parent if e.get(_q("Name")) == name), None)
                if old is not None:
                    idx = list(parent).index(old)
                    parent.remove(old)
                    if src is not None:
                        parent.insert(idx, copy.deepcopy(src))
                elif src is not None:
                    parent.append(copy.deepcopy(src))
            parts[name] = source.parts[name]

        if project_items:
            for name in [n for n in self.project_items() if n not in source.parts]:
                del parts[name]
            for name in source.project_items():
                parts[name] = source.parts[name]
            for tag in ("SSIS:ConnectionManagers", "SSIS:DeploymentInfo/SSIS:ProjectConnectionParameters",
                        "SSIS:DeploymentInfo/SSIS:ProjectParameters"):
                self._replace_section(manifest, source.manifest, tag)

        # No XML declaration: SSISDB 15.0 rejects a manifest that has one ("'Project' element ... is missing").
        parts[MANIFEST] = (self.parts[MANIFEST][0], ET.tostring(manifest, encoding="utf-8", xml_declaration=False))
        parts[CONTENT_TYPES] = (self.parts[CONTENT_TYPES][0], self._merged_content_types(source))

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for zip_name, data in parts.values():
                z.writestr(zip_name, data)
        return out.getvalue()

    def without(self, packages: list[str]) -> bytes:
        """This project with `packages` removed (parts and manifest entries); everything else unchanged."""
        manifest = copy.deepcopy(self.manifest)
        for path in ("SSIS:Packages/SSIS:Package", "SSIS:DeploymentInfo/SSIS:PackageInfo/SSIS:PackageMetaData"):
            parent_path = path.rsplit("/", 1)[0]
            parent = manifest.find(parent_path, _NS)
            if parent is None:
                continue
            for e in list(parent):
                if e.get(_q("Name")) in packages:
                    parent.remove(e)
        parts = {k: v for k, v in self.parts.items() if k not in packages}
        # No XML declaration: SSISDB 15.0 rejects a manifest that has one.
        parts[MANIFEST] = (self.parts[MANIFEST][0], ET.tostring(manifest, encoding="utf-8", xml_declaration=False))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for zip_name, data in parts.values():
                z.writestr(zip_name, data)
        return out.getvalue()

    @staticmethod
    def _replace_section(target: ET.Element, source: ET.Element, path: str) -> None:
        parent_path, _, _ = path.rpartition("/")
        tparent = target.find(parent_path, _NS) if parent_path else target
        if tparent is None:
            return
        old, new = target.find(path, _NS), source.find(path, _NS)
        if old is not None:
            idx = list(tparent).index(old)
            tparent.remove(old)
            if new is not None:
                tparent.insert(idx, copy.deepcopy(new))
        elif new is not None:
            tparent.append(copy.deepcopy(new))

    def _merged_content_types(self, source: "Ispac") -> bytes:
        if CONTENT_TYPES not in self.parts:
            return source.parts.get(CONTENT_TYPES, ("", b""))[1]
        mine = ET.fromstring(self.parts[CONTENT_TYPES][1])
        if CONTENT_TYPES in source.parts:
            have = {e.get("Extension", "").lower() for e in mine if e.tag.endswith("Default")}
            for e in ET.fromstring(source.parts[CONTENT_TYPES][1]):
                if e.tag.endswith("Default") and e.get("Extension", "").lower() not in have:
                    mine.append(copy.deepcopy(e))
        return ET.tostring(mine, encoding="utf-8", xml_declaration=True)
