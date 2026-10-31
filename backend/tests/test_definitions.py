"""Static checks on template.yaml and the state machine definition.

These catch wiring mistakes before `sam deploy`: broken references, missing
environment variables, handlers that don't exist, API routes the Lambda
doesn't handle, ASL states that point nowhere, etc.
"""
import ast
import importlib
import json
import os
import re

import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "backend", "src")


class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"__tag__": tag_suffix, "value": value}


CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template():
    with open(os.path.join(ROOT, "template.yaml")) as f:
        return yaml.load(f, Loader=CfnLoader)


@pytest.fixture(scope="module")
def asl():
    with open(os.path.join(ROOT, "statemachine", "leave_approval.asl.json")) as f:
        return json.load(f)


def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


PSEUDO = {"AWS::Region", "AWS::StackName", "AWS::AccountId", "AWS::Partition", "AWS::URLSuffix"}


def test_all_references_resolve(template):
    known = set(template["Resources"]) | set(template.get("Parameters", {})) | PSEUDO
    problems = []
    for node in _walk(template):
        if not (isinstance(node, dict) and "__tag__" in node):
            continue
        tag, value = node["__tag__"], node["value"]
        if tag == "Ref":
            if value not in known:
                problems.append(f"!Ref {value}")
        elif tag == "GetAtt":
            res = value.split(".")[0] if isinstance(value, str) else value[0]
            if res not in template["Resources"]:
                problems.append(f"!GetAtt {value}")
        elif tag == "Sub":
            text = value if isinstance(value, str) else value[0]
            for ref in re.findall(r"\$\{([^}!]+)\}", text):
                if ref.split(".")[0] not in known:
                    problems.append(f"!Sub ${{{ref}}}")
    assert problems == []


def _functions(template):
    return {name: r for name, r in template["Resources"].items() if r["Type"] == "AWS::Serverless::Function"}


def test_handlers_exist_and_import(template):
    for name, res in _functions(template).items():
        module, func = res["Properties"]["Handler"].rsplit(".", 1)
        mod = importlib.import_module(module)
        assert callable(getattr(mod, func)), name


def _env_vars_read(module_path, seen=None):
    """os.environ["X"] / os.environ.get("X") reads in a module and the common modules it imports."""
    seen = seen if seen is not None else set()
    if module_path in seen:
        return set()
    seen.add(module_path)
    path = os.path.join(SRC, *module_path.split(".")) + ".py"
    tree = ast.parse(open(path).read())
    names = _direct_env_reads(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "common":
            for alias in node.names:
                names |= _env_vars_read(f"common.{alias.name}", seen)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("common."):
            names |= _env_vars_read(node.module, seen)
    return names


def _direct_env_reads(tree):
    """Literal os.environ["X"] reads, plus table env names passed to db._table("X")."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ" \
                and isinstance(node.slice, ast.Constant):
            names.add(node.slice.value)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "_table" and node.args \
                and isinstance(node.args[0], ast.Constant):
            names.add(node.args[0].value)
    return names


# Required env vars that only some code paths of a shared module need
OPTIONAL_PER_FUNCTION = {
    "STATE_MACHINE_ARN", "SIGNING_SECRET_ARN", "APPROVAL_TOPIC_ARN", "DECISION_BASE_URL", "REPORTS_BUCKET",
    "SIGNING_SECRET",  # local testing only; Lambdas use SIGNING_SECRET_ARN
}


def test_required_env_vars_are_defined(template):
    global_env = set(template["Globals"]["Function"]["Environment"]["Variables"])
    for name, res in _functions(template).items():
        module = res["Properties"]["Handler"].rsplit(".", 1)[0]
        own = set(res["Properties"].get("Environment", {}).get("Variables", {}))
        defined = global_env | own
        handler_reads = _env_vars_read(module)
        direct = _direct_env_reads(ast.parse(open(os.path.join(SRC, *module.split(".")) + ".py").read()))
        must_have = (handler_reads - OPTIONAL_PER_FUNCTION) | direct
        missing = must_have - defined
        assert not missing, f"{name} is missing env vars {missing}"


def test_function_specific_env(template):
    fns = _functions(template)
    env = lambda n: set(fns[n]["Properties"].get("Environment", {}).get("Variables", {}))  # noqa: E731
    assert "STATE_MACHINE_ARN" in env("SubmitLeaveFunction")
    assert {"SIGNING_SECRET_ARN", "APPROVAL_TOPIC_ARN", "DECISION_BASE_URL"} <= env("NotifyApproverFunction")
    assert "SIGNING_SECRET_ARN" in env("DecisionLinkFunction")
    assert "REPORTS_BUCKET" in env("ApiFunction")


def test_api_routes_match_api_handler(template):
    src = open(os.path.join(SRC, "handlers", "api.py")).read()
    handled = set(re.findall(r'route == \("(\w+)", "([^"]+)"\)', src))
    events = _functions(template)["ApiFunction"]["Properties"]["Events"]
    declared = {(e["Properties"]["Method"].upper(), e["Properties"]["Path"]) for e in events.values()}
    assert handled == declared


def test_gsi_names_used_in_code_exist(template):
    gsis = {g["IndexName"] for g in template["Resources"]["LeaveRequestsTable"]["Properties"]["GlobalSecondaryIndexes"]}
    used = set()
    for root, _, files in os.walk(SRC):
        for f in files:
            if f.endswith(".py"):
                used |= set(re.findall(r'IndexName="([^"]+)"', open(os.path.join(root, f)).read()))
    assert used and used <= gsis


def test_key_attributes_defined(template):
    for name, res in template["Resources"].items():
        if res["Type"] != "AWS::DynamoDB::Table":
            continue
        props = res["Properties"]
        defined = {a["AttributeName"] for a in props["AttributeDefinitions"]}
        used = {k["AttributeName"] for k in props["KeySchema"]}
        for g in props.get("GlobalSecondaryIndexes", []):
            used |= {k["AttributeName"] for k in g["KeySchema"]}
        assert defined == used, name  # CloudFormation rejects unused attribute definitions


# ---------------------------------------------------------------- ASL

def test_asl_states_are_wired(asl):
    states = asl["States"]
    assert asl["StartAt"] in states
    targets = set()
    for name, s in states.items():
        for key in ("Next", "Default"):
            if key in s:
                targets.add(s[key])
        for c in s.get("Choices", []):
            targets.add(c["Next"])
        for c in s.get("Catch", []):
            targets.add(c["Next"])
        if s["Type"] in ("Task", "Pass") and "Next" not in s:
            assert s.get("End"), f"{name} has no Next/End"
    assert targets <= set(states), targets - set(states)
    assert set(states) - {asl["StartAt"]} <= targets, "unreachable states"


def test_asl_substitutions_match_template(asl, template):
    used = set(re.findall(r"\$\{(\w+)\}", json.dumps(asl)))
    subs = set(template["Resources"]["LeaveApprovalStateMachine"]["Properties"]["DefinitionSubstitutions"])
    assert used == subs


def test_asl_custom_errors_exist_in_handlers(asl):
    custom = set()
    for s in asl["States"].values():
        for c in s.get("Catch", []):
            custom |= {e for e in c["ErrorEquals"] if not e.startswith("States.")}
    classes = set()
    for f in ("notify_approver.py", "final_approve.py"):
        tree = ast.parse(open(os.path.join(SRC, "handlers", f)).read())
        classes |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert custom <= classes, custom - classes


def test_wait_states_have_timeouts(asl):
    for name, s in asl["States"].items():
        if s.get("Resource", "").endswith(".waitForTaskToken"):
            assert "TimeoutSecondsPath" in s or "TimeoutSeconds" in s, name
            assert any("States.Timeout" in c["ErrorEquals"] for c in s["Catch"]), name
            assert s["Parameters"]["Payload"]["taskToken.$"] == "$$.Task.Token"
