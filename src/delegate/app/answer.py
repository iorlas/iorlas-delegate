"""The answer of a `run`: the contract 3 object (`result`), its validation against the caller's JSON schema (`schema_problem`), and the one JSON line
on stdout (`emit`)."""
import json

from delegate.contracts import codec, stops, wire
from delegate.domain.cost import accounting


def schema_problem(data, schema, at="$"):
    """None when `data` fits the JSON schema subset delegate checks (type, required, properties, items, enum), else what is wrong."""
    if not isinstance(schema, dict):
        return None
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None), "number": (int, float), "integer": int}
    want = schema.get("type")
    want = want if isinstance(want, list) else [want] if want else []
    if want and not any(isinstance(data, kinds.get(w, object)) and not (w in ("number", "integer") and isinstance(data, bool)) for w in want):
        return f"{at} is not {'/'.join(want)}"
    if "enum" in schema and data not in schema["enum"]:
        return f"{at} is not one of {schema['enum']}"
    if isinstance(data, dict):
        for k in schema.get("required") or []:
            if k not in data:
                return f"{at}.{k} is missing"
        for k, sub in (schema.get("properties") or {}).items():
            if k in data:
                why = schema_problem(data[k], sub, f"{at}.{k}")
                if why:
                    return why
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for i, x in enumerate(data):
            why = schema_problem(x, schema["items"], f"{at}[{i}]")
            if why:
                return why
    return None


def parse_data(text, schema):
    if not schema or not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def result(outcome=None, **kw):
    """The contract 3 answer. `outcome` is the contract.json reason behind a non-done answer (contracts/stops.py); None = done, or the agent-tool
    handoff (`agent_model` set). `stop` is null exactly when outcome is None; its facts (scope, permanence, retry) come from the reason's row, and
    `retry_after` (a kw, never an answer field) becomes `stop.retry.at`."""
    retry_after, fix = kw.pop("retry_after", None), kw.pop("fix", None)
    resumable, partial = kw.pop("resumable", None), kw.pop("partial", None)
    stop = None
    if outcome:
        stop = stops.stop_facts(outcome, None if outcome == "logged_out" else retry_after, fix)      # logged_out ends when the owner logs in, not at a time
        stop["ref"] = kw.get("ref")        # ref on every outcome that has one
        if outcome in ("network", "killed"):       # resumable: the vendor session can be continued; partial: work was done before the stop
            stop.update(resumable=bool(resumable), partial=bool(partial))
    r = {"contract": stops.CONTRACT["contract"], "data": None, "text": "", "ref": None, "lane": None, "model": None, "effort": None, "family": None,
         "flag": None, "cost": 0, "tokens": None, "denied": [], "secs": 0, "run_id": None, "transcript": None, "say": "", "git_violation": [],
         "call_id": None, "transcript_info": None, "transcript_status": None, "stop": stop,
         "usage": {"tokens": None, "cost": 0, "secs": 0, "turns": None, "context_k": None, "answer_bytes": 0, **accounting.fields(None, None, 0)}}
    r.update(kw)
    return r


def emit(r, interactive):
    """One object on stdout. A model reads it when a Claude session is behind us, so empty fields are dropped there; scripts get every field.
    The answer passes through RunResult first: a key or a type the contract does not know is a bug here, never an answer."""
    r = codec.to_json(codec.from_json(wire.RunResult, r))
    if interactive:
        r = {k: v for k, v in r.items() if k in ("contract", "stop") or (v not in (None, "", [], 0) and k not in ("context", "usage", "transcript_info", "transcript_status"))}     # these are for scripts
    print(json.dumps(r))
    return 0        # exit 0 whenever the object was printed; non-zero only if route.py itself crashed
