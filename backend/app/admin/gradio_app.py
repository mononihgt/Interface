from __future__ import annotations

from threading import Thread
from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from backend.app.db import get_connection
from backend.app.repositories.admin import AdminRepository
from backend.app.settings import Settings
from backend.app.services.export_jobs import create_export_job, run_export_job_background


class AssignmentControlValidationError(ValueError):
    """User-facing validation error for admin assignment controls."""


def parse_cap_input(value: Any) -> int | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        parsed = int(normalized)
    except (TypeError, ValueError) as exc:
        raise AssignmentControlValidationError(
            "Cap must be a non-negative integer or left blank."
        ) from exc
    if parsed < 0:
        raise AssignmentControlValidationError(
            "Cap must be a non-negative integer or left blank."
        )
    return parsed


def get_assignment_form_values(
    summary: dict[str, Any],
    *,
    participant_type: str,
    condition: str,
    subcondition: str,
    error_type_id: str,
) -> dict[str, Any]:
    participant_summary = summary.get("participant_types", {}).get(participant_type, {})
    cells = participant_summary.get("cells", [])
    selected_cell = next(
        (
            cell
            for cell in cells
            if cell.get("condition") == condition
            and cell.get("subcondition") == subcondition
            and cell.get("error_type_id") == error_type_id
        ),
        None,
    )
    cap = None if selected_cell is None else selected_cell.get("cap")
    return {
        "cap_text": "" if cap is None else str(cap),
        "enabled": True if selected_cell is None else bool(selected_cell.get("enabled", True)),
    }


def build_admin_blocks(*, settings: Settings):
    import gradio as gr

    def with_repository(fn):
        def runner(*args):
            conn = get_connection(settings)
            try:
                repository = AdminRepository(conn, settings=settings)
                return fn(repository, *args)
            finally:
                conn.close()

        return runner

    @with_repository
    def load_overview(repository: AdminRepository) -> dict[str, Any]:
        return repository.get_overview_metrics()

    @with_repository
    def search_participants(repository: AdminRepository, query: str) -> dict[str, Any]:
        return repository.search_participants(query=query)

    @with_repository
    def load_assignment(
        repository: AdminRepository,
        participant_type: str,
        condition: str,
        subcondition: str,
        error_type_id: str,
    ) -> tuple[dict[str, Any], str, bool]:
        summary = repository.get_assignment_control_summary()
        form_values = get_assignment_form_values(
            summary,
            participant_type=participant_type,
            condition=condition,
            subcondition=subcondition,
            error_type_id=error_type_id,
        )
        return (
            summary,
            form_values["cap_text"],
            form_values["enabled"],
        )

    @with_repository
    def update_assignment_cell(
        repository: AdminRepository,
        participant_type: str,
        condition: str,
        subcondition: str,
        error_type_id: str,
        cap_value: Any,
        enabled: bool,
    ) -> tuple[dict[str, Any], str, bool]:
        try:
            summary = repository.update_assignment_controls(
                admin_user=settings.admin_user,
                operation="cell",
                participant_type=participant_type,
                condition=condition,
                subcondition=subcondition,
                error_type_id=error_type_id,
                cap=parse_cap_input(cap_value),
                enabled=enabled,
            )
        except AssignmentControlValidationError as exc:
            raise gr.Error(str(exc)) from exc
        form_values = get_assignment_form_values(
            summary,
            participant_type=participant_type,
            condition=condition,
            subcondition=subcondition,
            error_type_id=error_type_id,
        )
        return (
            summary,
            form_values["cap_text"],
            form_values["enabled"],
        )

    @with_repository
    def load_health(repository: AdminRepository) -> dict[str, Any]:
        return repository.get_api_health_summary()

    @with_repository
    def load_clean_data_audits(repository: AdminRepository, status: str) -> dict[str, Any]:
        normalized_status = "" if status == "all" else status
        try:
            return repository.list_clean_data_audits(status=normalized_status)
        except ValueError as exc:
            raise gr.Error(str(exc)) from exc

    @with_repository
    def recompute_clean_data_audits(repository: AdminRepository) -> dict[str, Any]:
        return repository.recompute_clean_data_audits(admin_user=settings.admin_user)

    @with_repository
    def run_deepseek_test(repository: AdminRepository) -> dict[str, object]:
        return repository.test_deepseek(admin_user=settings.admin_user)

    @with_repository
    def create_queued_export_job(
        repository: AdminRepository,
        export_type: str,
        include_test: bool,
    ) -> tuple[dict[str, Any], str | None]:
        conn = repository._conn
        try:
            if export_type == "reimbursement" and include_test:
                raise gr.Error("reimbursement exports do not support test sessions.")
            job = create_export_job(
                conn,
                export_type=export_type,
                filters={},
                include_test=include_test,
                created_by=settings.admin_user,
            )
            Thread(
                target=run_export_job_background,
                kwargs={
                    "settings": settings,
                    "job_uuid": str(job["job_uuid"]),
                },
                daemon=True,
            ).start()
        except ValueError as exc:
            raise gr.Error(str(exc)) from exc
        return job, None

    def create_all_data_export_job(include_test: bool) -> tuple[dict[str, Any], str | None]:
        return create_queued_export_job("experiment_data", include_test)

    def create_clean_export_job() -> tuple[dict[str, Any], str | None]:
        return create_queued_export_job("complete_no_external_error_data", False)

    @with_repository
    def load_logs_with_download(repository: AdminRepository) -> tuple[dict[str, Any], str]:
        payload = repository.get_system_logs_summary()
        return payload, payload["sanitized_package_path"]

    with gr.Blocks(title="Admin Console") as blocks:
        gr.Markdown("## Gradio Admin Console")
        gr.Markdown(
            "On-screen admin responses stay sanitized; export archives intentionally keep participant phone numbers and interface-compatible audio files."
        )

        with gr.Tab("Overview"):
            overview_refresh = gr.Button("Refresh overview", variant="primary")
            overview_output = gr.JSON(label="Overview metrics")
            overview_refresh.click(load_overview, outputs=overview_output)
            blocks.load(load_overview, outputs=overview_output)

        with gr.Tab("Participants"):
            participant_query = gr.Textbox(
                label="Search by participant id, name, or phone last four digits"
            )
            participant_search = gr.Button("Search participants", variant="primary")
            participant_output = gr.JSON(label="Sanitized participant rows")
            participant_search.click(
                search_participants,
                inputs=participant_query,
                outputs=participant_output,
            )
            blocks.load(search_participants, inputs=participant_query, outputs=participant_output)

        with gr.Tab("Assignment Control"):
            with gr.Row():
                participant_type_input = gr.Dropdown(
                    choices=["short", "long"],
                    value="short",
                    label="Participant type",
                )
                condition_input = gr.Dropdown(
                    choices=["human", "tool"],
                    value="human",
                    label="Condition",
                )
                subcondition_input = gr.Dropdown(
                    choices=["qa", "planning", "chat", "decision", "execution"],
                    value="qa",
                    label="Subcondition",
                )
                error_type_input = gr.Dropdown(
                    choices=[
                        "factual_minor",
                        "factual_major",
                        "logic_minor",
                        "logic_major",
                        "social_minor",
                        "social_major",
                        "system_failure",
                    ],
                    value="factual_minor",
                    label="Error type",
                )
            with gr.Row():
                cap_input = gr.Textbox(
                    label="Cap",
                    placeholder="blank for no cap",
                )
                enabled_input = gr.Checkbox(
                    label="Enabled",
                    value=True,
                )
                submit_cell = gr.Button("Save cell control", variant="primary")
            assignment_refresh = gr.Button("Refresh assignment summary", variant="primary")
            assignment_output = gr.JSON(label="Assignment control")
            assignment_outputs = [
                assignment_output,
                cap_input,
                enabled_input,
            ]
            assignment_selector_inputs = [
                participant_type_input,
                condition_input,
                subcondition_input,
                error_type_input,
            ]
            submit_cell.click(
                update_assignment_cell,
                inputs=[
                    *assignment_selector_inputs,
                    cap_input,
                    enabled_input,
                ],
                outputs=assignment_outputs,
            )
            assignment_refresh.click(
                load_assignment,
                inputs=assignment_selector_inputs,
                outputs=assignment_outputs,
            )
            blocks.load(
                load_assignment,
                inputs=assignment_selector_inputs,
                outputs=assignment_outputs,
            )
            participant_type_input.change(
                load_assignment,
                inputs=assignment_selector_inputs,
                outputs=assignment_outputs,
            )
            condition_input.change(
                load_assignment,
                inputs=assignment_selector_inputs,
                outputs=assignment_outputs,
            )
            subcondition_input.change(
                load_assignment,
                inputs=assignment_selector_inputs,
                outputs=assignment_outputs,
            )
            error_type_input.change(
                load_assignment,
                inputs=assignment_selector_inputs,
                outputs=assignment_outputs,
            )

        with gr.Tab("Clean Data Audit"):
            clean_data_status = gr.Dropdown(
                choices=["all", "eligible", "review_needed", "excluded"],
                value="all",
                label="Status",
            )
            clean_data_refresh = gr.Button("Refresh clean data audits", variant="primary")
            clean_data_recompute = gr.Button("Recompute clean data audits")
            clean_data_output = gr.JSON(label="Clean data audits")
            clean_data_refresh.click(
                load_clean_data_audits,
                inputs=clean_data_status,
                outputs=clean_data_output,
            )
            clean_data_recompute.click(
                recompute_clean_data_audits,
                outputs=clean_data_output,
            )
            clean_data_status.change(
                load_clean_data_audits,
                inputs=clean_data_status,
                outputs=clean_data_output,
            )
            blocks.load(
                load_clean_data_audits,
                inputs=clean_data_status,
                outputs=clean_data_output,
            )

        with gr.Tab("Export Jobs"):
            export_job_include_test = gr.Checkbox(
                label="Include test sessions in all-data export",
                value=False,
            )
            with gr.Row():
                export_all_data_button = gr.Button("Export all data", variant="primary")
                export_clean_data_button = gr.Button("Export complete_no_external_error_data", variant="secondary")
            export_job_output = gr.JSON(label="Export job status")
            export_job_download = gr.File(label="Completed archive")
            export_all_data_button.click(
                create_all_data_export_job,
                inputs=export_job_include_test,
                outputs=[export_job_output, export_job_download],
            )
            export_clean_data_button.click(
                create_clean_export_job,
                inputs=None,
                outputs=[export_job_output, export_job_download],
            )

        with gr.Tab("API Health"):
            health_refresh = gr.Button("Refresh API health", variant="primary")
            health_output = gr.JSON(label="API health summary")
            health_refresh.click(load_health, outputs=health_output)
            blocks.load(load_health, outputs=health_output)

        with gr.Tab("Provider Test"):
            provider_run = gr.Button("Test DeepSeek", variant="primary")
            provider_output = gr.JSON(label="Sanitized DeepSeek test result")
            provider_run.click(run_deepseek_test, outputs=provider_output)

        with gr.Tab("System Logs"):
            logs_refresh = gr.Button("Refresh sanitized logs", variant="primary")
            logs_output = gr.JSON(label="System log summary")
            logs_download = gr.File(label="Sanitized log package")
            logs_refresh.click(
                load_logs_with_download,
                outputs=[logs_output, logs_download],
            )
            blocks.load(load_logs_with_download, outputs=[logs_output, logs_download])

    return blocks


def mount_admin_console(app: FastAPI, *, settings: Settings) -> FastAPI:
    try:
        import gradio as gr
    except ImportError:
        fallback = FastAPI()

        def _dependency_error_response() -> JSONResponse:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": "Admin console dependency error: gradio is not installed."
                },
            )

        @app.get("/admin/console")
        def admin_dependency_error_root() -> JSONResponse:
            return _dependency_error_response()

        @fallback.get("/{path:path}")
        def admin_dependency_error(path: str = "") -> JSONResponse:
            return _dependency_error_response()

        app.mount("/admin/console", fallback)
        return app

    blocks = build_admin_blocks(settings=settings)
    try:
        return gr.mount_gradio_app(
            app,
            blocks,
            path="/admin/console",
        )
    except TypeError:
        return gr.mount_gradio_app(app, blocks, "/admin/console")
