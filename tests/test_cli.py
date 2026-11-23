from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

import kdl


def _runCli(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    stdinText: str | None = None,
) -> None:
    monkeypatch.setattr(sys, "argv", ["kdlreformat", *argv])
    if stdinText is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdinText))
    kdl.cli()


def test_cli_stdin_to_stdout_and_formatting_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = 'parent  b=2 a=1 {\n  child 0x1a 1e-5 bare "quoted" "a\\\\b" #"raw"# #"a\\nb"#\n}\n'

    # Default flags: tab indent, preserve radix, --string-type=auto, lowercase e, keep prop order
    _runCli(monkeypatch, [], stdinText=src)
    captured = capsys.readouterr()
    assert captured.out == 'parent b=2 a=1 {\n\tchild 0x1a 1e-5 bare quoted #"a\\b"# #"raw"# #"a\\nb"#\n}\n'
    assert captured.err == ""

    # Explicit --string-type=plain and other formatting flags
    _runCli(
        monkeypatch,
        [
            "--indent",
            "2",
            "--semicolons",
            "--no-radix",
            "--string-type",
            "plain",
            "--exponent",
            "E",
            "--sort-properties",
            "-",
            "-",
        ],
        stdinText=src,
    )
    captured = capsys.readouterr()
    assert captured.out == 'parent a=1 b=2 {\n  child 26 1E-5 bare quoted "a\\\\b" raw "a\\\\nb";\n};\n'

    # --string-type=exact preserves exact string value types while printing node/prop names as idents when possible
    _runCli(
        monkeypatch,
        ["--indent", "0", "--radix", "--string-type", "exact", "--exponent", "e"],
        stdinText=src,
    )
    captured = capsys.readouterr()
    assert captured.out == 'parent b=2 a=1 {\nchild 0x1a 1e-5 bare "quoted" "a\\\\b" #"raw"# #"a\\nb"#\n}\n'

    # --string-type=auto explicit flag
    _runCli(monkeypatch, ["--string-type", "auto"], stdinText=src)
    assert capsys.readouterr().out == 'parent b=2 a=1 {\n\tchild 0x1a 1e-5 bare quoted #"a\\b"# #"raw"# #"a\\nb"#\n}\n'

    # --raw-strings (alias for exact) and --no-raw-strings (alias for plain)
    rawSrc = 'node "q" r#"a\\nb"#\n'
    _runCli(monkeypatch, ["--raw-strings"], stdinText=rawSrc)
    assert capsys.readouterr().out == 'node "q" #"a\\nb"#\n'
    _runCli(monkeypatch, ["--no-raw-strings"], stdinText=rawSrc)
    assert capsys.readouterr().out == 'node q "a\\\\nb"\n'


def test_cli_file_io_and_inplace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inFile = tmp_path / "in.kdl"
    outFile = tmp_path / "out.kdl"
    inFile.write_text('node  "hello"  b=2 a=1\n', encoding="utf-8")

    # infile -> stdout
    _runCli(monkeypatch, [str(inFile)])
    assert capsys.readouterr().out == "node hello b=2 a=1\n"

    # infile -> outfile
    _runCli(monkeypatch, ["--sort-properties", str(inFile), str(outFile)])
    assert outFile.read_text(encoding="utf-8") == "node hello a=1 b=2\n"

    # in-place reformat (infile == outfile)
    _runCli(monkeypatch, ["--sort-properties", str(inFile), str(inFile)])
    assert inFile.read_text(encoding="utf-8") == "node hello a=1 b=2\n"


def test_cli_version_flag_and_parse_error_preserves_outfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inFile = tmp_path / "v1.kdl"
    outFile = tmp_path / "out.kdl"
    inFile.write_text("node true\n", encoding="utf-8")
    outFile.write_text("keep-me\n", encoding="utf-8")

    # --version 2 rejects KDL1 syntax, exits 1, reports ParseError on stderr, and preserves outfile
    with pytest.raises(SystemExit) as excInfo:
        _runCli(monkeypatch, ["--version", "2", str(inFile), str(outFile)])
    assert excInfo.value.code == 1
    assert "ParseError:" in capsys.readouterr().err
    assert outFile.read_text(encoding="utf-8") == "keep-me\n"

    # In-place parse error must also preserve the input file
    inFile.write_text("node [\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excInfo:
        _runCli(monkeypatch, [str(inFile), str(inFile)])
    assert excInfo.value.code == 1
    assert "ParseError:" in capsys.readouterr().err
    assert inFile.read_text(encoding="utf-8") == "node [\n"

    # --version 1 and --version auto accept KDL1 input
    inFile.write_text("node true\n", encoding="utf-8")
    _runCli(monkeypatch, ["--version", "1", str(inFile), str(outFile)])
    assert outFile.read_text(encoding="utf-8") == "node #true\n"

    _runCli(monkeypatch, ["--version", "auto", str(inFile)])
    assert capsys.readouterr().out == "node #true\n"


def test_cli_invalid_arguments_and_os_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Invalid --exponent
    with pytest.raises(SystemExit) as excInfo:
        _runCli(monkeypatch, ["--exponent", "x"])
    assert excInfo.value.code == 2
    assert "Expected 'e' or 'E'" in capsys.readouterr().err

    # Invalid --version
    with pytest.raises(SystemExit) as excInfo:
        _runCli(monkeypatch, ["--version", "3"])
    assert excInfo.value.code == 2
    assert "Expected '1', '2', or 'auto'" in capsys.readouterr().err

    # Invalid --string-type
    with pytest.raises(SystemExit) as excInfo:
        _runCli(monkeypatch, ["--string-type", "bogus"])
    assert excInfo.value.code == 2
    assert "Expected 'auto', 'exact', or 'plain'" in capsys.readouterr().err

    # Missing input file -> concise OSError on stderr, exit 1
    missing = tmp_path / "does_not_exist.kdl"
    with pytest.raises(SystemExit) as excInfo:
        _runCli(monkeypatch, [str(missing)])
    assert excInfo.value.code == 1
    assert "Error:" in capsys.readouterr().err

    # Output path is a directory -> concise OSError on stderr, exit 1
    validIn = tmp_path / "ok.kdl"
    validIn.write_text("node 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excInfo:
        _runCli(monkeypatch, [str(validIn), str(tmp_path)])
    assert excInfo.value.code == 1
    assert "Error:" in capsys.readouterr().err


def test_kdlreformat_entrypoint_script() -> None:
    scriptPath = Path(__file__).resolve().parent.parent / "kdlreformat.py"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(scriptPath)],
        input='a  "b"  1\n',
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout == "a b 1\n"
