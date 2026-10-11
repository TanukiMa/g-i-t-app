"""Store a JSON data file as ONE RECORD PER LINE, so that a change shows WHICH record changed.

`json_prettify` writes every field of a record on its own line (the MHLW supply-status report: 24 lines per drug, keys in
alphabetical order). A change of `shipment_status_nm` is then a single line, and the name of the drug is several lines
away: neither the diff nor the AI summary says which drug it was. With

    json_records: {id: yj_cd, label: product_nm}      # in config.yaml, per site (never written to website-stalker.yaml)

the stored file keeps the structure but writes the records of the (largest) list of objects one per line:

    {
    "data": [
    {"product_nm": "...", "yj_cd": "...", "component_nm": "...", ...},
    ...
    ],
    "total": 16409
    }

- the records are sorted by `id` (a list that the server returns in another order is not a change),
- inside a record the `label` field comes first, then `id`, then the other keys in alphabetical order,
- it is idempotent: the output read again gives the same file, so the step may run in every pipeline run,
- text that is not JSON (an error page) or has no list of objects is left untouched.
It runs right after website-stalker, before git looks for changes (`website_stalk.process_site`).
"""
import json
import os


def _largest_list_key(data: dict):
    """Key of the longest list of objects in a dict, or None."""
    best, best_len = None, 0
    for key, value in data.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) and len(value) > best_len:
            best, best_len = key, len(value)
    return best


def _line(record: dict, id_key: str, label_key: str) -> str:
    first = [k for k in (label_key, id_key) if k and k in record]
    ordered = {k: record[k] for k in first}
    ordered.update({k: record[k] for k in sorted(record) if k not in ordered})
    return json.dumps(ordered, ensure_ascii=False)


def format_records(text: str, id_key: str, label_key: str = ""):
    """The reformatted text, or None when `text` is not a JSON object with a list of objects (then nothing is changed)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    list_key = _largest_list_key(data)
    if list_key is None:
        return None
    lines = sorted(((str(r.get(id_key, "")), _line(r, id_key, label_key)) for r in data[list_key]))
    out = ["{"]
    keys = list(data)
    for n, key in enumerate(keys):
        comma = "," if n < len(keys) - 1 else ""
        if key == list_key:
            out.append(f"{json.dumps(key, ensure_ascii=False)}: [")
            out.extend(line + ("," if i < len(lines) - 1 else "") for i, (_id, line) in enumerate(lines))
            out.append("]" + comma)
        else:
            out.append(f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(data[key], ensure_ascii=False)}{comma}")
    out.append("}")
    return "\n".join(out) + "\n"


def apply_to_site(site_path: str, spec: dict) -> int:
    """Reformat every .json file below `site_path`; returns how many files were rewritten."""
    id_key, label_key = str(spec.get("id") or ""), str(spec.get("label") or "")
    rewritten = 0
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8", newline="") as f:
                text = f.read()
            new = format_records(text, id_key, label_key)
            if new is None:
                print(f"json_records: {name} is not a JSON object with a list of records; left as it is.")
            elif new != text:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new)
                rewritten += 1
    return rewritten
