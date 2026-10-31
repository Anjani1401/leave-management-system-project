"""A small Step Functions interpreter for tests.

It runs the real statemachine/leave_approval.asl.json against the real Lambda
handlers, supporting the subset of ASL the definition uses: Task
(lambda:invoke and lambda:invoke.waitForTaskToken), Choice, Succeed, Fail,
Parameters with ".$" paths, ResultSelector, ResultPath and Catch.

A waitForTaskToken task pauses the run. The test then either sends a decision
(through the real decision endpoint, which calls SendTaskSuccess on the fake
SFN client) and calls resume(), or calls timeout() to simulate the deadline.
"""
import copy
import itertools
import json
import os

from handlers import final_approve, notify_approver, reject

ASL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "statemachine", "leave_approval.asl.json")

FUNCTIONS = {
    "${NotifyApproverFunctionArn}": notify_approver.handler,
    "${FinalApproveFunctionArn}": final_approve.handler,
    "${RejectFunctionArn}": reject.handler,
}

_token_counter = itertools.count(1)


def load_definition():
    with open(ASL_PATH) as f:
        return json.load(f)


def get_path(data, path, context):
    if path == "$":
        return data
    if path.startswith("$$."):
        node = context
        parts = path[3:].split(".")
    elif path.startswith("$."):
        node = data
        parts = path[2:].split(".")
    else:
        raise ValueError(f"Unsupported path {path}")
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"States.Runtime: path {path} not found")
        node = node[part]
    return node


def resolve(template, data, context):
    if isinstance(template, dict):
        out = {}
        for key, value in template.items():
            if key.endswith(".$"):
                out[key[:-2]] = copy.deepcopy(get_path(data, value, context))
            else:
                out[key] = resolve(value, data, context)
        return out
    if isinstance(template, list):
        return [resolve(v, data, context) for v in template]
    return template


def set_result(data, result_path, result):
    if result_path is None:
        return data
    if result_path == "$":
        return result
    if not result_path.startswith("$."):
        raise ValueError(result_path)
    out = copy.deepcopy(data)
    node = out
    parts = result_path[2:].split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = result
    return out


def choice_matches(rule, data):
    if "And" in rule:
        return all(choice_matches(r, data) for r in rule["And"])
    if "Or" in rule:
        return any(choice_matches(r, data) for r in rule["Or"])
    if "Not" in rule:
        return not choice_matches(rule["Not"], data)
    try:
        value = get_path(data, rule["Variable"], {})
    except KeyError:
        raise RuntimeError(f"States.Runtime: Choice variable {rule['Variable']} missing")
    if "StringEquals" in rule:
        return value == rule["StringEquals"]
    if "BooleanEquals" in rule:
        return value is rule["BooleanEquals"]
    raise NotImplementedError(rule)


class Execution:
    def __init__(self, workflow_input, sfn_fake, definition=None):
        self.definition = definition or load_definition()
        self.data = copy.deepcopy(workflow_input)
        self.state = self.definition["StartAt"]
        self.sfn = sfn_fake
        self.status = "RUNNING"
        self.path = []          # visited states, like the console graph
        self.waiting_token = None
        self.output = None

    def _catch(self, state_def, error_name, cause):
        for catcher in state_def.get("Catch", []):
            names = catcher["ErrorEquals"]
            if error_name in names or "States.ALL" in names or (
                    "States.TaskFailed" in names and error_name != "States.Timeout"):
                self.data = set_result(self.data, catcher.get("ResultPath", "$"),
                                       {"Error": error_name, "Cause": cause})
                self.state = catcher["Next"]
                return True
        self.status = "FAILED"
        self.output = {"Error": error_name, "Cause": cause}
        return False

    def run(self):
        """Run until the workflow finishes or waits for a task token."""
        while self.status == "RUNNING":
            name = self.state
            sdef = self.definition["States"][name]
            self.path.append(name)
            kind = sdef["Type"]
            if kind == "Succeed":
                self.status, self.output = "SUCCEEDED", self.data
            elif kind == "Fail":
                self.status, self.output = "FAILED", {"Error": sdef.get("Error"), "Cause": sdef.get("Cause")}
            elif kind == "Choice":
                for rule in sdef["Choices"]:
                    if choice_matches(rule, self.data):
                        self.state = rule["Next"]
                        break
                else:
                    self.state = sdef["Default"]
            elif kind == "Task":
                if self._run_task(name, sdef) == "WAIT":
                    return self
            else:
                raise NotImplementedError(kind)
        return self

    def _run_task(self, name, sdef):
        wait = sdef["Resource"].endswith(".waitForTaskToken")
        token = f"token-{next(_token_counter)}" if wait else None
        context = {"Task": {"Token": token}}
        params = resolve(sdef["Parameters"], self.data, context)
        fn = FUNCTIONS[params["FunctionName"]]
        if wait:
            # TimeoutSecondsPath must resolve to an integer
            timeout = get_path(self.data, sdef["TimeoutSecondsPath"], context)
            assert isinstance(timeout, int) and timeout > 0, "TimeoutSecondsPath must be a positive integer"
        try:
            payload = fn(copy.deepcopy(params["Payload"]), None)
            json.dumps(payload)  # Lambda output must be JSON serialisable
        except Exception as exc:  # noqa: BLE001
            self._catch(sdef, type(exc).__name__, str(exc))
            return None
        if wait:
            self.waiting_token = token
            self._waiting_def = (name, sdef)
            return "WAIT"
        result = {"Payload": payload, "StatusCode": 200}
        if "ResultSelector" in sdef:
            result = resolve(sdef["ResultSelector"], result, context)
        self.data = set_result(self.data, sdef.get("ResultPath", "$"), result)
        self.state = sdef["Next"] if "Next" in sdef else None
        if sdef.get("End"):
            self.status = "SUCCEEDED"
        return None

    def resume(self):
        """Continue after the decision endpoint called SendTaskSuccess."""
        token = self.waiting_token
        assert token, "Execution is not waiting"
        assert token in self.sfn.task_results, "No SendTaskSuccess received for the waiting token"
        name, sdef = self._waiting_def
        output = self.sfn.task_results[token]
        self.sfn.closed_tokens.add(token)
        self.waiting_token = None
        self.data = set_result(self.data, sdef.get("ResultPath", "$"), output)
        self.state = sdef["Next"]
        return self.run()

    def timeout(self):
        """Simulate the task timeout (TimeoutSecondsPath elapsed with no decision)."""
        token = self.waiting_token
        assert token, "Execution is not waiting"
        name, sdef = self._waiting_def
        self.sfn.closed_tokens.add(token)
        self.waiting_token = None
        if self._catch(sdef, "States.Timeout", "Task timed out"):
            return self.run()
        return self
