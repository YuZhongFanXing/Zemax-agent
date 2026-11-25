import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path

import clr


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_api():
    configured = os.environ.get("OPTICSTUDIO_INSTALL")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates += [
        Path(r"D:\Program Files\Ansys Zemax OpticStudio 2023 R1.00"),
        Path(r"C:\Program Files\Ansys Zemax OpticStudio"),
        Path.home() / "Desktop" / "OpticStudio_API_Test",
    ]
    names = ("ZOSAPI_NetHelper.dll", "ZOSAPI_Interfaces.dll", "ZOSAPI.dll")
    install = next((path for path in candidates if all((path / name).is_file() for name in names)), candidates[0])
    required = [install / name for name in ("ZOSAPI_NetHelper.dll", "ZOSAPI_Interfaces.dll", "ZOSAPI.dll")]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("opticstudio_runtime_missing:" + ";".join(missing) + ";searched=" + ",".join(str(p) for p in candidates))
    print(f"zosapi_install={install}")
    sys.path.insert(0, str(install))
    clr.AddReference(str(install / "ZOSAPI_NetHelper.dll"))
    from ZOSAPI_NetHelper import ZOSAPI_Initializer
    if not ZOSAPI_Initializer.Initialize():
        raise RuntimeError("zos_initializer_failed")
    clr.AddReference(str(install / "ZOSAPI_Interfaces.dll"))
    clr.AddReference(str(install / "ZOSAPI.dll"))
    import ZOSAPI
    return ZOSAPI


def new_application(api):
    app = api.ZOSAPI_Connection().CreateNewApplication()
    if app is None or not app.IsValidLicenseForAPI or app.PrimarySystem is None:
        if app is not None:
            app.CloseApplication()
        raise RuntimeError("zos_application_unavailable")
    return app


def load_adapter(path):
    spec = importlib.util.spec_from_file_location("model_adapter", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def export_object(system, api, object_id, output_path, file_type):
    tool = system.Tools.OpenExportCAD()
    try:
        tool.FirstObject = int(object_id)
        tool.LastObject = int(object_id)
        tool.FileType = file_type
        try:
            tool.Tolerance = api.Tools.General.CADToleranceType.N_TenEMinus6
            tool.AngularTolerance = api.Tools.General.CADAngularToleranceType.High
            tool.SurfacesAsSolids = True
            tool.ExportDummySurfaces = False
        except Exception:
            pass
        tool.OutputFileName = str(output_path)
        run_result = tool.RunAndWaitForCompletion()
        if not tool.Succeeded or not Path(output_path).is_file():
            message = getattr(tool, "ErrorMessage", "")
            status = getattr(tool, "Status", "")
            raise RuntimeError(f"cad_export_failed:run={run_result},status={status},error={message},path={output_path}")
    finally:
        tool.Close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    args = parser.parse_args()
    request_path = Path(args.request).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    output = Path(request["output_dir"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    adapter = load_adapter(request_path.parent / request["model_adapter"])
    api = load_api()

    probe_path = output / request.get("probe_filename", "probe.zos")
    app = new_application(api)
    try:
        app.PrimarySystem.MakeNonSequential()
        app.PrimarySystem.SaveAs(str(probe_path))
    finally:
        app.CloseApplication()
    app = new_application(api)
    try:
        app.PrimarySystem.LoadFile(str(probe_path), False)
        probe_count = int(app.PrimarySystem.NCE.NumberOfObjects)
    finally:
        app.CloseApplication()
    probe = {"ok": probe_count >= 1, "reopened_objects": probe_count, "zos_sha256": digest(probe_path)}
    write_json(output / "native_result_probe.json", probe)
    if not probe["ok"]:
        raise RuntimeError("probe_failed")

    model_path = output / request["model_filename"]
    app = new_application(api)
    try:
        system = app.PrimarySystem
        system.MakeNonSequential()
        # Optional fourth argument lets a model-specific adapter create
        # temporary NSC systems and copy native parents. Inspect the declared
        # signature instead of catching TypeError from inside the adapter.
        parameters = list(inspect.signature(adapter.build).parameters.values())
        accepts_app = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters)
        accepts_app = accepts_app or len(parameters) >= 4
        build_metadata = (adapter.build(system, api, request, app) if accepts_app
                          else adapter.build(system, api, request))
        system.SaveAs(str(model_path))
    finally:
        app.CloseApplication()

    app = new_application(api)
    try:
        system = app.PrimarySystem
        system.LoadFile(str(model_path), False)
        readback = adapter.readback(system, api, request, build_metadata)
        # Persist readback before CAD export. If a CAD tool rejects the chosen
        # native representation, the next direct Codex iteration still has
        # complete object/type/parameter evidence to select another one.
        write_json(output / "native_readback.json", {
            "probe": probe,
            "build": build_metadata,
            "model": readback,
            "model_path": str(model_path),
            "model_sha256": digest(model_path),
        })
        if readback.get("pass") is False:
            write_json(output / "native_readback_failure.json", {
                "error": "native_readback_assertions_failed",
                "model": readback,
                "native_readback": str(output / "native_readback.json"),
            })
            raise RuntimeError("native_readback_assertions_failed")
        if not request.get("export_step", True) and not request.get("export_stl", True):
            write_json(output / request.get("result_filename", "native_result.json"), {
                "ok": True, "probe": probe, "model": readback,
                "model_sha256": digest(model_path),
                "cad_exports": "skipped_openability_only",
            })
            return
        final_id = int(readback["final_object_id"])
        step_path = output / request["step_filename"]
        stl_path = output / request["stl_filename"]
        optional_exports = request.get("optional_cad_exports", False)
        export_failures = {}
        if request.get("export_step", True):
            try:
                export_object(system, api, final_id, step_path, api.Tools.General.CADFileType.STEP)
            except Exception as exc:
                write_json(output / "cad_export_failure.json", {
                    "file_type": "STEP", "object_id": final_id,
                    "object_type": readback.get("object_type"),
                    "output_path": str(step_path), "error": str(exc),
                    "native_readback": str(output / "native_readback.json"),
                })
                if not optional_exports or request.get("require_step"):
                    raise
                export_failures["STEP"] = str(exc)
        else:
            write_json(output / "cad_export_skipped.json", {
                "file_type": "STEP", "reason": "request.export_step=false",
                "native_readback": str(output / "native_readback.json"),
            })
        if request.get("export_stl", True):
            try:
                export_object(system, api, final_id, stl_path, api.Tools.General.CADFileType.STL)
            except Exception as exc:
                write_json(output / "cad_export_failure.json", {
                    "file_type": "STL", "object_id": final_id,
                    "object_type": readback.get("object_type"),
                    "output_path": str(stl_path), "error": str(exc),
                    "native_readback": str(output / "native_readback.json"),
                })
                if not optional_exports or request.get("require_stl"):
                    raise
                export_failures["STL"] = str(exc)
    finally:
        app.CloseApplication()

    result = {"ok": True, "probe": probe, "model": readback,
              "model_sha256": digest(model_path)}
    if optional_exports:
        result["cad_export_failures"] = export_failures
    if step_path.is_file() and "STEP" not in export_failures:
        result["step_sha256"] = digest(step_path)
    if stl_path.is_file() and "STL" not in export_failures:
        result["stl_sha256"] = digest(stl_path)
    write_json(output / request.get("result_filename", "native_result.json"), result)


if __name__ == "__main__":
    main()
