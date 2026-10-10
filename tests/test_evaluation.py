"""Report formatting and CLI behavior using the existing evaluation results."""

import json
import sys

import pytest

from kite_x402_fastapi import Settings
from kite_x402_fastapi import evaluation
from kite_x402_fastapi.evaluation import Check, EvaluationCase, EvaluationReport, PAY_TO


def test_markdown_success_report():
    report = EvaluationReport("eip155:2366", "USDC.e", "$0.001", (
        EvaluationCase("unpaid", "Rejects unpaid requests", (Check("HTTP status", True, 402, 402),)),
        EvaluationCase("paid", "Verifies, calls upstream, then settles", (Check("HTTP status", True, 200, 200),)),
    ))
    assert report.to_markdown() == "\n".join([
        "# x402 service evaluation",
        "",
        "**PASSED**: 2/2 scenarios passed; 0 failed.",
        "",
        "- Network: eip155:2366",
        "- Asset: USDC.e",
        "- Price: $0.001",
        "",
        "| ID | Scenario | Status |",
        "| --- | --- | --- |",
        "| unpaid | Rejects unpaid requests | PASSED |",
        "| paid | Verifies, calls upstream, then settles | PASSED |",
    ])


def test_markdown_failed_checks_and_errors():
    report = EvaluationReport("eip155:2366", "USDC.e", "$0.001", (
        EvaluationCase("unpaid", "Rejects unpaid requests", (Check("HTTP status", True, 402, 402),)),
        EvaluationCase("paid", "Settles after upstream success", (
            Check("HTTP status", True, 200, 200),
            Check("response body", False, b"expected", b"actual"),
            Check("operation order", False, {"z": "settle", "a": "upstream"}, {"z": "upstream", "a": "settle"}),
        )),
        EvaluationCase("crashed", "Scenario raises an exception", (), error="RuntimeError: upstream failed"),
    ))
    markdown = report.to_markdown()
    assert "**FAILED**: 1/3 scenarios passed; 2 failed." in markdown
    assert "| unpaid | Rejects unpaid requests | PASSED |" in markdown
    assert "| paid | Settles after upstream success | FAILED |" in markdown
    assert "| crashed | Scenario raises an exception | FAILED |" in markdown
    assert '## paid: Settles after upstream success\n\n- response body: expected "expected", got "actual".' in markdown
    assert '- operation order: expected {"a": "upstream", "z": "settle"}, got {"a": "settle", "z": "upstream"}.' in markdown
    assert "- Error: RuntimeError: upstream failed" in markdown
    assert "HTTP status" not in markdown


def test_markdown_escapes_special_characters():
    report = EvaluationReport("eip155:2366", "USDC.e", "$0.001", (
        EvaluationCase("case_id", "A | B\r\n<service> *name* `code` [link] \\ path", (),
                       error="RuntimeError: first | line\nsecond & final"),
    ))
    markdown = report.to_markdown()
    assert "| case\\_id | A \\| B<br>&lt;service&gt; \\*name\\* \\`code\\` \\[link\\] \\\\ path | FAILED |" in markdown
    assert "- Error: RuntimeError: first \\| line<br>second &amp; final" in markdown


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
def test_markdown_evaluation_is_deterministic(network):
    settings = Settings(pay_to=PAY_TO, upstream_url="https://upstream.example/base", kite_network=network)
    report = evaluation.evaluate_service(settings=settings)
    markdown = report.to_markdown()
    assert report.passed
    assert "**PASSED**: 10/10 scenarios passed; 0 failed." in markdown
    assert all(f"| {case.name} | PASSED |" in markdown for case in report.cases)
    assert markdown == evaluation.evaluate_service(settings=settings).to_markdown()


@pytest.mark.parametrize("output_format", ["text", "json", "markdown"])
@pytest.mark.parametrize("passed", [True, False])
def test_cli_output_and_exit_status(monkeypatch, capsys, output_format, passed):
    report = EvaluationReport("eip155:2366", "USDC.e", "$0.001", (
        EvaluationCase("paid", "Verifies, calls upstream, then settles", (
            Check("HTTP status", passed, 200, 200 if passed else 402),
        )),
    ))
    monkeypatch.setattr(evaluation, "evaluate_service", lambda **kwargs: report)
    monkeypatch.setattr(sys, "argv", ["kite-x402-evaluate", "--format", output_format])
    with pytest.raises(SystemExit) as exit_info:
        evaluation.main()
    assert exit_info.value.code == (0 if passed else 1)
    output = capsys.readouterr().out
    if output_format == "json":
        assert json.loads(output) == report.to_dict()
    else:
        expected = report.to_markdown() if output_format == "markdown" else report.to_text()
        assert output == expected + "\n"
