from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .assets import LocalAssetStorage
from .models import (
    AIIntentParseRequest,
    AIIntentParseResponse,
    AssetRecord,
    FeedbackCreateRequest,
    FeedbackReceipt,
    ModelingRequestCreate,
    ModelingRequestInput,
    ModelingRequestRecord,
    ParsedDrawingModel,
    ProjectCreateRequest,
    ProjectSummary,
    VersionSnapshot,
)
from .pipeline import ModelingPipeline
from .store import SQLiteStore, Store


repo_root = Path(__file__).resolve().parents[1]
app_root = Path(__file__).resolve().parent
web_dist = repo_root / "web" / "dist"
default_data_root = repo_root / "data"
default_export_root = repo_root / "generated"


def create_app(
    *,
    runtime_root: Path | None = None,
    store: Store | None = None,
    export_root: Path | None = None,
    asset_root: Path | None = None,
) -> FastAPI:
    resolved_runtime_root = runtime_root or default_data_root
    resolved_export_root = export_root or (
        resolved_runtime_root / "generated" if runtime_root is not None else default_export_root
    )
    resolved_asset_root = asset_root or resolved_runtime_root / "assets"
    resolved_store = store or SQLiteStore(resolved_runtime_root / "jianmo.sqlite3")
    asset_storage = LocalAssetStorage(resolved_asset_root)
    pipeline = ModelingPipeline(store=resolved_store, export_root=resolved_export_root)

    app = FastAPI(
        title="建筑自动建模系统 MVP",
        version="0.2.0",
        description="依据建筑自动建模设计文档实现的首版编排服务。",
    )
    app.state.pipeline = pipeline
    app.state.asset_storage = asset_storage
    app.state.runtime_root = resolved_runtime_root

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/projects", response_model=ProjectSummary)
    def create_project(payload: ProjectCreateRequest) -> ProjectSummary:
        return pipeline.create_project(payload)

    @app.get("/api/projects", response_model=list[ProjectSummary])
    def list_projects() -> list[ProjectSummary]:
        return pipeline.list_projects()

    @app.get("/api/projects/{project_id}", response_model=ProjectSummary)
    def get_project(project_id: str) -> ProjectSummary:
        project = pipeline.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    @app.post("/api/projects/{project_id}/assets", response_model=AssetRecord)
    async def upload_asset(
        project_id: str,
        file: UploadFile = File(...),
        description: str | None = Form(default=None),
    ) -> AssetRecord:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if not file.filename:
            raise HTTPException(status_code=400, detail="Uploaded file must have a filename")

        content = await file.read()
        await file.close()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        stored = asset_storage.save(
            project_id,
            filename=file.filename,
            media_type=file.content_type or "application/octet-stream",
            content=content,
        )
        return pipeline.create_asset(
            project_id,
            filename=stored.filename,
            media_type=stored.media_type,
            description=description,
            path=stored.path,
            extension=stored.extension,
            size_bytes=stored.size_bytes,
            content_hash=stored.content_hash,
        )

    @app.get("/api/projects/{project_id}/assets", response_model=list[AssetRecord])
    def list_assets(project_id: str) -> list[AssetRecord]:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return pipeline.list_assets(project_id)

    @app.post("/api/projects/{project_id}/requests", response_model=ModelingRequestRecord)
    def create_request(project_id: str, payload: ModelingRequestCreate) -> ModelingRequestRecord:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            return pipeline.create_request(project_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Asset not found: {exc.args[0]}") from exc

    @app.get("/api/projects/{project_id}/requests", response_model=list[ModelingRequestRecord])
    def list_requests(project_id: str) -> list[ModelingRequestRecord]:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return pipeline.list_requests(project_id)

    @app.get("/api/projects/{project_id}/requests/{request_id}", response_model=ModelingRequestRecord)
    def get_request(project_id: str, request_id: str) -> ModelingRequestRecord:
        request = pipeline.get_request(project_id, request_id)
        if request is None:
            raise HTTPException(status_code=404, detail="Request not found")
        return request

    @app.post("/api/projects/{project_id}/requests/{request_id}/parse", response_model=ParsedDrawingModel)
    def parse_request(project_id: str, request_id: str) -> ParsedDrawingModel:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            return pipeline.parse_request(project_id, request_id)
        except KeyError as exc:
            detail = "Request not found" if exc.args and exc.args[0] == request_id else f"Asset not found: {exc.args[0]}"
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post("/api/projects/{project_id}/requests/{request_id}/run", response_model=VersionSnapshot)
    def run_request(project_id: str, request_id: str) -> VersionSnapshot:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            return pipeline.run_request(project_id, request_id)
        except KeyError as exc:
            detail = "Request not found" if exc.args and exc.args[0] == request_id else f"Asset not found: {exc.args[0]}"
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post("/api/projects/{project_id}/modeling-requests", response_model=VersionSnapshot)
    def run_modeling_request(project_id: str, payload: ModelingRequestInput) -> VersionSnapshot:
        try:
            return pipeline.run(project_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @app.post("/api/projects/{project_id}/intent/parse", response_model=AIIntentParseResponse)
    def parse_intent(project_id: str, payload: AIIntentParseRequest) -> AIIntentParseResponse:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            return pipeline.parse_intent_only(project_id, payload)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/versions", response_model=list[VersionSnapshot])
    def list_versions(project_id: str) -> list[VersionSnapshot]:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return pipeline.list_versions(project_id)

    @app.get("/api/projects/{project_id}/versions/{version_id}", response_model=VersionSnapshot)
    def get_version(project_id: str, version_id: str) -> VersionSnapshot:
        version = pipeline.get_version(project_id, version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Version not found")
        return version

    @app.get(
        "/api/projects/{project_id}/versions/{version_id}/artifacts/{artifact_name:path}",
        include_in_schema=False,
    )
    def get_version_artifact(project_id: str, version_id: str, artifact_name: str) -> FileResponse:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            artifact = pipeline.get_export_artifact(project_id, version_id, artifact_name)
        except KeyError as exc:
            detail = "Version not found" if exc.args and exc.args[0] == version_id else "Artifact not found"
            raise HTTPException(status_code=404, detail=detail) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact file not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return FileResponse(path=artifact.path, media_type=artifact.media_type, filename=artifact.name)

    @app.post(
        "/api/projects/{project_id}/versions/{version_id}/feedbacks",
        response_model=FeedbackReceipt,
    )
    def submit_version_feedback(
        project_id: str,
        version_id: str,
        payload: FeedbackCreateRequest,
    ) -> FeedbackReceipt:
        if pipeline.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            return pipeline.submit_feedback(project_id, version_id, payload)
        except KeyError as exc:
            detail = "Version not found" if exc.args and exc.args[0] == version_id else "Project not found"
            raise HTTPException(status_code=404, detail=detail) from exc

    if web_dist.exists():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def web_app(full_path: str) -> FileResponse:
            candidate = web_dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        def web_placeholder() -> HTMLResponse:
            return HTMLResponse(
                """
                <!DOCTYPE html>
                <html lang="zh-CN">
                  <head>
                    <meta charset="utf-8" />
                    <meta name="viewport" content="width=device-width, initial-scale=1" />
                    <title>建筑自动建模系统 MVP</title>
                  </head>
                  <body>
                    <main>
                      <h1>建筑自动建模系统 MVP</h1>
                      <p>前端构建产物尚未生成，请先在 <code>jianmo/web</code> 中执行 <code>npm run build</code>，或使用 Vite 开发服务器访问前端。</p>
                    </main>
                  </body>
                </html>
                """
            )

    return app


app = create_app()


def get_dev_server_config() -> tuple[str, int]:
    host = os.getenv("JIANMO_APP_HOST", "0.0.0.0")
    port = int(os.getenv("JIANMO_APP_PORT", "3000"))
    return host, port


def run_dev_server() -> None:
    host, port = get_dev_server_config()
    uvicorn.run(
        "jianmo.app.main:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=[str(app_root)],
    )


if __name__ == "__main__":
    run_dev_server()
