from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_multi_job_ui_contract():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for required_id in (
        'id="job-summary"',
        'id="form-feedback"',
        'id="active-job-list"',
        'id="job-history-list"',
        'id="job-detail"',
        'id="job-log"',
    ):
        assert required_id in html
    assert "変換キューに追加" in html
    assert "Add to conversion queue" in html


def test_ui_has_cancel_and_list_api_calls():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'api("/api/local/jobs")' in script
    assert "/cancel" in script
    assert "currentJobId" not in script
    assert "selectedJobId" in script
    assert "jobsById" in script


def test_ui_exposes_all_text_statuses_and_exact_cancel_warning():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    for status in (
        "queued",
        "running",
        "cancelling",
        "cancelled",
        "completed",
        "completed_with_warnings",
        "failed",
    ):
        assert status in script
    assert (
        "この変換を停止します。途中まで作成されたデータは削除されます。"
        "ほかの変換と完了済みデータには影響しません。"
    ) in script


def test_ui_keeps_function_first_responsive_layout():
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "@media (max-width: 860px)" in styles
    assert ".summary-strip" in styles
    assert ".job-card" in styles
    assert "--danger" in styles


def test_job_refresh_is_single_flight_with_only_one_poller():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "let refreshPromise = null;" in script
    assert "if (refreshPromise) return refreshPromise;" in script
    assert "refreshPromise = (async () =>" in script
    assert "refreshPromise = null;" in script
    assert script.count("setInterval(") == 1


def test_cancel_error_is_shown_and_authoritative_state_is_refreshed():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    cancel_body = script.split("async function cancelJob", 1)[1].split("async function openOutput", 1)[0]
    assert "try {" in cancel_body
    assert "catch (error)" in cancel_body
    assert "setFormFeedback(error.message, true);" in cancel_body
    assert "finally {" in cancel_body
    assert "if (refreshPromise)" in cancel_body
    assert "await refreshPromise" in cancel_body
    assert "await refreshJobs();" in cancel_body


def test_job_lists_share_bounded_scroll_region():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert 'class="jobs-panel-scroll"' in html
    assert 'tabindex="0"' in html
    assert ".jobs-panel-scroll" in styles
    assert "position: absolute;" in styles
    assert "overflow-y: auto;" in styles
    assert "max-height: 70vh;" in styles
