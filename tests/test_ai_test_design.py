"""AI Test Design (Beta) and the Gemini fallback policy it shares with Dexter.

No network: Gemini, Jira and Confluence are replaced by fakes.  What is locked
here is everything that decides what a QA lead sees — which model is asked and
when it is skipped, what is read out of each source, and how the model's answer
is validated before it reaches the screen.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from src import confluence_client as cc
from src import gemini_client as gc
from src import jira_client as jc
from src import test_design as td


# ── the shared fallback policy ───────────────────────────────────────────────
class _FakeModels:
    def __init__(self, behaviour: dict):
        self.behaviour = behaviour
        self.calls: list[str] = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.behaviour[model]
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {"text": outcome})()


@pytest.fixture
def fake_gemini(monkeypatch):
    def install(behaviour: dict) -> _FakeModels:
        models = _FakeModels(behaviour)
        monkeypatch.setattr(gc, "AVAILABLE", True)
        monkeypatch.setattr(gc, "api_key", lambda: "test-key")
        monkeypatch.setattr(gc, "client", lambda key: type("C", (), {"models": models})())
        return models
    return install


class TestFallbackPolicy:
    CHAIN = ("best", "good", "last")

    def test_the_best_model_answers_first(self, fake_gemini):
        m = fake_gemini({"best": " ok ", "good": "no", "last": "no"})
        res = gc.generate([], None, self.CHAIN, {})
        assert (res.model, res.text, m.calls) == ("best", "ok", ["best"])

    def test_a_rate_limited_model_is_skipped_for_googles_retry_delay(self, fake_gemini):
        m = fake_gemini({"best": Exception("429 RESOURCE_EXHAUSTED retryDelay: '16s'"),
                         "good": "ok", "last": "no"})
        cooling: dict = {}
        res = gc.generate([], None, self.CHAIN, cooling)
        assert res.model == "good" and m.calls == ["best", "good"]
        assert 10 < cooling["best"] - gc.time.time() <= 16

    def test_no_quota_skips_the_model_for_a_day(self, fake_gemini):
        fake_gemini({"best": Exception("429 quota limit: 0"), "good": "ok", "last": "no"})
        cooling: dict = {}
        gc.generate([], None, self.CHAIN, cooling)
        assert cooling["best"] - gc.time.time() > 23 * 3600

    def test_overload_and_not_found_step_down_too(self, fake_gemini):
        m = fake_gemini({"best": Exception("503 UNAVAILABLE"),
                         "good": Exception("404 NOT_FOUND"), "last": "ok"})
        res = gc.generate([], None, self.CHAIN, {})
        assert res.model == "last" and m.calls == list(self.CHAIN)

    def test_a_real_error_stops_instead_of_burning_the_chain(self, fake_gemini):
        m = fake_gemini({"best": Exception("400 INVALID_ARGUMENT file too large"),
                         "good": "ok", "last": "ok"})
        res = gc.generate([], None, self.CHAIN, {})
        assert res.model is None and m.calls == ["best"]
        assert "INVALID_ARGUMENT" in gc.failure_message(res.error)

    def test_a_model_still_cooling_down_is_not_even_asked(self, fake_gemini):
        m = fake_gemini({"best": "no", "good": "ok", "last": "no"})
        res = gc.generate([], None, self.CHAIN, {"best": gc.time.time() + 60})
        assert res.model == "good" and m.calls == ["good"]

    def test_not_configured_says_so(self, monkeypatch):
        monkeypatch.setattr(gc, "api_key", lambda: None)
        res = gc.generate([], None, self.CHAIN, {})
        assert res.model is None and "GEMINI_API_KEY" in gc.failure_message(res.error)


class TestDexterSharesThePolicy:
    """Dexter's behaviour must not change: same chain, same retry parsing, and
    its reply path goes through the shared generate()."""

    def test_dexters_chain_is_unchanged(self):
        from src.ui import chat_assistant as ca
        assert ca._FALLBACK_CHAIN == ["gemini-2.5-flash", "gemini-2.5-flash-lite",
                                      "gemini-2.0-flash"]

    def test_dexter_uses_the_shared_implementation(self):
        import inspect

        from src.ui import chat_assistant as ca
        assert ca._parse_retry_delay is gc.parse_retry_delay
        src = inspect.getsource(ca._generate_pending_response)
        assert "gemini_client.generate(" in src
        assert "gemini_client.failure_message(" in src

    def test_both_features_share_one_cooldown_table(self):
        import inspect

        from src.ui import chat_assistant as ca
        from src.ui import test_design_tab as tab
        assert "gemini_client.COOLDOWN_KEY" in inspect.getsource(ca._generate_pending_response)
        assert "gemini_client.COOLDOWN_KEY" in inspect.getsource(tab._run)

    def test_test_design_starts_from_pro(self):
        assert td.MODEL_CHAIN == ["gemini-2.5-pro", "gemini-2.5-flash",
                                  "gemini-2.5-flash-lite"]


# ── reading the sources ──────────────────────────────────────────────────────
class TestJiraStoryReading:
    def test_keys_are_found_bare_in_urls_and_lowercase(self):
        text = ("IPXL20-15740, https://x.atlassian.net/browse/SD-512 and "
                "ipxl20-15740 again\nhttps://x.atlassian.net/jira/software/c/"
                "projects/TPS/boards/1?selectedIssue=TPS-9")
        assert jc.extract_issue_keys(text) == ["IPXL20-15740", "SD-512", "TPS-9"]

    def test_rich_text_becomes_readable(self):
        adf = {"type": "doc", "content": [
            {"type": "heading", "content": [{"type": "text", "text": "Acceptance criteria"}]},
            {"type": "orderedList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Code applies"}]},
                    {"type": "bulletList", "content": [
                        {"type": "listItem", "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "once"}]}]}]}]},
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "See "},
                        {"type": "inlineCard", "attrs": {"url": "https://spec"}},
                        {"type": "hardBreak"},
                        {"type": "mention", "attrs": {"text": "@PO"}}]}]}]},
            {"type": "table", "content": [
                {"type": "tableRow", "content": [
                    {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Input"}]}]},
                    {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Result"}]}]}]}]},
        ]}
        text = jc.adf_to_text(adf)
        assert "Acceptance criteria\n" in text
        assert "1. Code applies\n  - once" in text
        assert "2. See https://spec\n  @PO" in text
        assert "Input | Result" in text

    def test_a_story_with_an_ac_field_is_marked_so(self):
        s = td.story_source({"key": "SD-1", "summary": "Promo", "acceptance_criteria": "AC1 x",
                             "description": "", "attachments": 2})
        assert s.ok and not s.warn
        assert "acceptance criteria field" in s.note and "2 attachment(s) not read" in s.note
        assert "ACCEPTANCE CRITERIA FIELD:\nAC1 x" in s.text

    def test_ac_written_in_the_description_are_recognised(self):
        s = td.story_source({"key": "SD-1", "summary": "P", "acceptance_criteria": "",
                             "description": "Given a basket\nWhen I pay\nThen it works"})
        assert s.ok and not s.warn and "in the description" in s.note

    def test_a_story_without_ac_is_flagged_before_generation(self):
        s = td.story_source({"key": "SD-1", "summary": "P", "acceptance_criteria": "",
                             "description": "Make it nicer"})
        assert s.ok and s.warn and "no acceptance criteria found" in s.note

    def test_an_unreadable_story_is_reported_and_never_sent(self):
        s = td.story_source({"key": "SD-404", "error": "not found (or no permission)"})
        assert not s.ok
        assert td.request_parts([s]) == [td.request_parts([])[0]]


class TestConfluenceReading:
    def test_page_references_in_every_form(self):
        text = ("https://x.atlassian.net/wiki/spaces/QA/pages/123456/Checkout+spec, "
                "https://x.atlassian.net/wiki/pages/viewpage.action?pageId=777 "
                "98765 https://x.atlassian.net/wiki/x/AbC-d 123456")
        assert cc.extract_page_refs(text) == ["123456", "777", "98765", "x:AbC-d"]

    def test_storage_format_becomes_readable_without_macro_settings(self):
        html = ("<h2>Rules</h2><p>Code &amp; basket</p><ul><li>once</li><li>twice</li></ul>"
                "<table><tr><th>In</th><th>Out</th></tr><tr><td>A</td><td>B</td></tr></table>"
                '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">java'
                "</ac:parameter><ac:plain-text-body><![CDATA[x = 1]]></ac:plain-text-body>"
                "</ac:structured-macro>")
        text = cc.storage_to_text(html)
        assert "Rules" in text and "Code & basket" in text
        assert "- once" in text and "- twice" in text
        assert "In | Out" in text and "A | B" in text
        assert "x = 1" in text and "java" not in text


class TestFileReading:
    @staticmethod
    def _docx(paragraphs, table=None) -> bytes:
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        def p(t: str) -> str:
            return f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"

        body = "".join(p(t) for t in paragraphs)
        if table:
            body += "<w:tbl>" + "".join(
                "<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in row) + "</w:tr>"
                for row in table) + "</w:tbl>"
        xml = f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", xml)
        return buf.getvalue()

    def test_docx_paragraphs_and_tables_without_a_new_dependency(self):
        text = td.docx_text(self._docx(["AC1 Promo applies", "AC2 Once only"],
                                       table=[["Code", "Result"], ["X10", "-10%"]]))
        assert text.splitlines() == ["AC1 Promo applies", "AC2 Once only",
                                     "Code | Result", "X10 | -10%"]

    def test_xlsx_rows(self):
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.title = "AC"
        wb.active.append(["id", "criterion"])
        wb.active.append(["AC1", "Promo applies"])
        buf = io.BytesIO()
        wb.save(buf)
        text = td.xlsx_text(buf.getvalue())
        assert "## Sheet: AC" in text and "AC1 | Promo applies" in text

    def test_images_and_pdfs_are_sent_natively(self):
        img, pdf = td.read_file("mock.PNG", b"\x89PNG"), td.read_file("spec.pdf", b"%PDF")
        assert (img.ok, img.mime, pdf.ok, pdf.mime) == (True, "image/png", True, "application/pdf")
        parts = td.request_parts([img])
        assert parts[-1] == ("bytes", b"\x89PNG", "image/png")

    def test_unsupported_and_empty_files_are_reported(self):
        assert not td.read_file("a.exe", b"x").ok
        assert not td.read_file("empty.txt", b"   ").ok

    def test_a_huge_text_is_truncated_and_says_so(self):
        s = td.read_file("big.txt", b"a" * (td.MAX_TEXT_CHARS + 10))
        assert s.ok and s.warn and "truncated" in s.note and len(s.text) == td.MAX_TEXT_CHARS


# ── the request ──────────────────────────────────────────────────────────────
class TestRequest:
    def test_every_placeholder_is_filled(self):
        for device in td.DEVICES:
            for negative in (False, True):
                text = td.system_instruction(device, negative)
                assert "{" not in text and "}" not in text

    def test_negative_cases_are_opt_in(self):
        assert "Otherwise none" in td.system_instruction("Both", False)
        assert "Keep them few" in td.system_instruction("Both", True)

    def test_the_schema_is_accepted_by_the_sdk(self):
        pytest.importorskip("google.genai")
        from google.genai import types
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=td.response_schema())
        assert config.response_schema is not None


# ── the answer ───────────────────────────────────────────────────────────────
def _answer(**over) -> str:
    data = {
        "acceptance_criteria": [
            {"id": "a", "text": "Valid code applies 10%", "source": "SD-1"},
            {"id": "b", "text": "Code usable once", "source": "SD-1"},
            {"id": "c", "text": "Banner shows savings", "source": "SD-1"},
        ],
        "test_cases": [
            {"title": "User applies a valid promo code", "preconditions": "Basket with 1 item",
             "priority": "high", "type": "Functional", "device": "both",
             "covers": ["a", "zzz", "a"],
             "steps": [{"step": "Open basket", "expected": "Basket shown"},
                       {"step": "Apply <valid code>", "expected": "10% discount shown"}]},
            {"title": "User reuses a code", "preconditions": "", "priority": "urgent",
             "type": "", "device": "tablet", "covers": ["b"],
             "steps": [{"step": "Apply the code again", "expected": ""}]},
            {"title": "Broken test", "covers": ["c"], "steps": []},
        ],
        "open_questions": ["'Savings' amount format is not specified", ""],
    }
    data.update(over)
    return json.dumps(data)


class TestAnswerValidation:
    def test_ids_are_renumbered_and_references_follow(self):
        d = td.parse_design(_answer())
        assert [c["id"] for c in d.criteria] == ["AC1", "AC2", "AC3"]
        assert [t.id for t in d.tests] == ["TC1", "TC2"]
        assert d.tests[0].covers == ["AC1"]              # unknown and duplicate refs dropped
        assert d.tests_for("AC2") == ["TC2"]

    def test_an_uncovered_criterion_is_named(self):
        """AC3's only test had no steps: dropped, counted, and AC3 is flagged."""
        d = td.parse_design(_answer())
        assert d.dropped == 1 and d.uncovered == ["AC3"]

    def test_out_of_range_values_are_normalised(self):
        d = td.parse_design(_answer())
        assert (d.tests[0].priority, d.tests[0].device) == ("High", "Both")
        assert (d.tests[1].priority, d.tests[1].device, d.tests[1].type) == (
            "Medium", "Both", "Functional")

    def test_empty_questions_are_dropped(self):
        assert td.parse_design(_answer()).questions == [
            "'Savings' amount format is not specified"]

    def test_code_fences_and_chatter_around_the_json_are_tolerated(self):
        d = td.parse_design("Here you go:\n```json\n" + _answer() + "\n```")
        assert len(d.tests) == 2

    def test_not_json_is_an_error_not_a_crash(self):
        with pytest.raises(td.DesignError, match="not valid JSON"):
            td.parse_design("Sorry, I can't help with that.")

    def test_no_usable_test_case_is_an_error(self):
        with pytest.raises(td.DesignError, match="no usable test case"):
            td.parse_design(_answer(test_cases=[{"title": "x", "steps": []}]))

    def test_plain_text_copy_has_every_step_and_question(self):
        text = td.to_text(td.parse_design(_answer()))
        assert "TC1 · User applies a valid promo code" in text
        assert "  2. Apply <valid code>\n     Expected: 10% discount shown" in text
        assert "Covers: AC1" in text and "Open questions:" in text


class TestGenerate:
    def _sources(self):
        return [td.story_source({"key": "SD-1", "summary": "Promo",
                                 "acceptance_criteria": "AC1 x", "description": ""})]

    def test_a_good_answer_becomes_a_design(self, fake_gemini):
        pytest.importorskip("google.genai")
        m = fake_gemini({"gemini-2.5-pro": _answer(), "gemini-2.5-flash": "",
                         "gemini-2.5-flash-lite": ""})
        out = td.generate(self._sources(), "Both", False, {})
        assert out.model == "gemini-2.5-pro" and len(out.design.tests) == 2
        assert m.calls == ["gemini-2.5-pro"]

    def test_pro_without_quota_falls_back_to_flash(self, fake_gemini):
        pytest.importorskip("google.genai")
        m = fake_gemini({"gemini-2.5-pro": Exception("429 limit: 0"),
                         "gemini-2.5-flash": _answer(), "gemini-2.5-flash-lite": ""})
        out = td.generate(self._sources(), "Desktop", True, {})
        assert out.model == "gemini-2.5-flash" and out.design is not None
        assert m.calls == ["gemini-2.5-pro", "gemini-2.5-flash"]

    def test_an_unreadable_answer_keeps_the_raw_text(self, fake_gemini):
        pytest.importorskip("google.genai")
        fake_gemini({"gemini-2.5-pro": "not json", "gemini-2.5-flash": "",
                     "gemini-2.5-flash-lite": ""})
        out = td.generate(self._sources(), "Both", False, {})
        assert out.design is None and out.raw == "not json" and "valid JSON" in out.error

    def test_nothing_readable_never_calls_gemini(self, fake_gemini):
        m = fake_gemini({})
        out = td.generate([td.Source("jira", "SD-9", False, "no permission")], "Both", False, {})
        assert out.design is None and m.calls == []


# ── the tab ──────────────────────────────────────────────────────────────────
class TestTab:
    """Rendered for real with Streamlit's test runner — no network involved."""

    @staticmethod
    def _app():
        from streamlit.testing.v1 import AppTest

        def page():
            from src.ui import test_design_tab
            test_design_tab.render()

        return AppTest.from_function(page, default_timeout=30)

    def test_without_a_gemini_key_it_explains_instead_of_failing(self):
        at = self._app().run()
        assert not at.exception
        assert any("GEMINI_API_KEY" in i.value for i in at.info)

    def test_with_a_key_the_form_is_drawn_and_unconfigured_sources_are_disabled(self):
        pytest.importorskip("google.genai")
        at = self._app()
        at.secrets["GEMINI_API_KEY"] = "test-key"
        at.run()
        assert not at.exception
        labels = {t.label: t for t in at.text_area}
        assert {"Jira stories", "Confluence pages", "Notes"} <= set(labels)
        assert labels["Jira stories"].disabled and labels["Confluence pages"].disabled
        assert not labels["Notes"].disabled

    def test_generating_with_no_source_asks_for_one(self):
        pytest.importorskip("google.genai")
        at = self._app()
        at.secrets["GEMINI_API_KEY"] = "test-key"
        at.run()
        at.button[0].click().run()
        assert not at.exception
        assert any("at least one source" in w.value for w in at.warning)
