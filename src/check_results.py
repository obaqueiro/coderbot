"""Conservative result adapters: unknown failures cannot become preexisting."""
import json
import re
import xml.etree.ElementTree as ET


def parse_output(output, exit_code, reporter="text"):
    if exit_code and re.search(r"ModuleNotFoundError: No module named|ImportError: Failed to import test module", output):
        return {"status": "infrastructure", "failures": {}, "exit_code": exit_code}
    failures = {}
    recognized = exit_code == 0
    if reporter == "unittest":
        recognized = bool(re.search(r"Ran \d+ tests? in", output))
        for match in re.finditer(r"(?:FAIL|ERROR): (.+?)\n[-]+\n(.*?)(?=\n[=]{5,}|\n[-]{5,}\nRan |\Z)", output, re.S):
            failures[match[1].strip()] = match[2].strip()
    elif reporter == "node":
        recognized = bool(re.search(r"# (?:tests|pass) \d+", output))
        for match in re.finditer(r"not ok \d+ - (.+?)\n(.*?)(?=\n(?:ok |not ok |#)|\Z)", output, re.S):
            failures[match[1].strip()] = match[2].strip()
    elif reporter == "eslint":
        # ESLint's default stylish format. Require a complete summary and account
        # for every error; a truncated/unfamiliar format remains unknown.
        text = re.sub(r"\x1b\[[0-9;]*m", "", output)
        summary = re.search(r"\d+ problems? \((\d+) errors?, \d+ warnings?\)", text)
        current = None
        for line in text.splitlines():
            if line.startswith("/"):
                current = line.strip()
            match = re.match(r"\s+(\d+):(\d+)\s+error\s+(.+?)\s{2,}(\S+)\s*$", line)
            if current and match:
                identity = f"{current}:{match[1]}:{match[2]}:{match[4]}"
                failures[identity] = match[3]
        recognized = bool(summary and int(summary[1]) == len(failures))
    elif reporter == "junit":
        try:
            root = ET.fromstring(output)
            recognized = True
            for case in root.iter("testcase"):
                for error in list(case):
                    if error.tag in ("failure", "error"):
                        failures[case.get("classname", "") + ":" + case.get("name", "")] = (
                            error.get("message", "") + "\n" + (error.text or "")).strip()
        except ET.ParseError:
            recognized = False
    elif reporter == "json":
        try:
            value = json.loads(output)
            failures = value["failures"]
            recognized = isinstance(failures, dict) and all(isinstance(v, str) for v in failures.values())
        except (ValueError, KeyError, TypeError):
            recognized = False
    elif reporter == "playwright":
        recognized = bool(re.search(r"\d+ (?:failed|passed)\b", output))
        for match in re.finditer(r"^\s*\d+\) (\[.+?\].+?)\n(.*?)(?=^\s*\d+\) \[|^\s*\d+ (?:failed|passed)|\Z)", output, re.M | re.S):
            test = re.sub(r":\d+:\d+", ":<line>", match[1].strip())
            # Retain assertion/error context, exclude artifact paths and timing.
            detail = match[2].split("attachment #", 1)[0].strip()
            failures[test] = detail
        summary = re.search(r"^\s*(\d+) failed\b", output, re.M)
        if exit_code and summary and int(summary[1]) != len(failures):
            recognized = False  # Partial failures cannot establish baseline equivalence.
    # Playwright list output is not a stable identity contract. Use JSON/JUnit via
    # an explicit plan for baseline attribution; zero exit is still a real pass.
    status = "pass" if exit_code == 0 else "fail" if recognized and failures else "unknown"
    result = {"status": status, "failures": failures, "exit_code": exit_code}
    count = test_count(output, reporter)
    if count is not None:
        result["test_count"] = count
    if status == "pass" and _zero_tests(output, reporter):
        # Informational only: the status stays "pass" so nothing downstream changes.
        # final_checks decides (behind CODEBOT_EMPTY_SUITE_FAILS) whether it matters.
        result.update(zero_tests=True, detail="0 tests executed")
    return result


def test_count(output, reporter):
    """Positive execution evidence from a declared test reporter, never exit code alone."""
    try:
        pattern = {"unittest": r"^Ran (\d+) tests? in", "node": r"^# tests (\d+)\s*$",
                   "playwright": r"^\s*(\d+) (?:passed|failed|flaky)\b"}.get(reporter)
        if pattern:
            counts = re.findall(pattern, output, re.M)
            if counts:
                return sum(map(int, counts)) if reporter == "playwright" else int(counts[-1])
            return None
        if reporter == "junit":
            return sum(not any(child.tag == "skipped" for child in case) for case in ET.fromstring(output).iter("testcase"))
        if reporter == "json":
            value = json.loads(output).get("ran")
            return value if type(value) is int and value >= 0 else None
    except (ValueError, TypeError, AttributeError, ET.ParseError):
        pass
    return None


def _zero_tests(output, reporter):
    """True only on an explicit zero count from a recognized reporter (never a guess):
    unrecognized output keeps meaning what it meant before."""
    try:
        if reporter == "unittest":
            ran = re.findall(r"^Ran (\d+) tests? in", output, re.M)
            if not ran:
                return False
            count = int(ran[-1])
            skipped = re.search(r"^OK \(skipped=(\d+)\)\s*$", output, re.M)
            return count == 0 or bool(skipped and int(skipped[1]) >= count)
        if reporter == "node":
            tests = re.search(r"^# tests (\d+)\s*$", output, re.M)
            passed = re.search(r"^# pass (\d+)\s*$", output, re.M)
            return bool(tests and int(tests[1]) == 0) or bool(passed and int(passed[1]) == 0
                                                               and not re.search(r"^# fail [1-9]", output, re.M))
        if reporter == "junit":
            cases = list(ET.fromstring(output).iter("testcase"))
            return not cases or all(any(child.tag == "skipped" for child in case) for case in cases)
        if reporter == "json":
            value = json.loads(output)
            return isinstance(value, dict) and value.get("ran") == 0
        if reporter == "playwright":
            return (bool(re.search(r"\b\d+ skipped\b", output))
                    and not re.search(r"\b\d+ (?:passed|failed|flaky)\b", output))
    except (ValueError, TypeError, ET.ParseError):
        return False
    return False
