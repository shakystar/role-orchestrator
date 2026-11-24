"""Value and feedback rules shared by handoff inputs and outputs."""

JSON_TYPES = {'object': (dict,), 'array': (list,), 'string': (str,),
              'integer': (int,), 'number': (int, float), 'boolean': (bool,),
              'null': (type(None),)}


def field_problems(payload, fields):
    problems = []
    for path, kind in fields.items():
        value = payload
        for part in path.split('.'):
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif (isinstance(value, list) and part.isascii() and part.isdecimal()
                  and len(part) < 20 and int(part) < len(value)):
                value = value[int(part)]
            else:
                problems.append(dict(field_path=path, observed='missing', expected=kind or 'present'))
                break
        else:
            if kind and type(value) not in JSON_TYPES[kind]:
                problems.append(dict(field_path=path, observed=type(value).__name__, expected=kind))
    return problems


def field_condition(problems):
    if len(problems) == 1:
        issue = problems[0]
        path = issue['field_path']
        return [False, 'structure', path, issue['observed'], 'provide ' + path, 'field is ' + issue['expected']]
    return [False, 'structure', '$', problems, 'correct all listed fields', 'each field meets its expected type or presence']


def semantic_key(condition):
    # The observation explains a defect. Its wording does not identify the repair.
    _, criterion, field, _, required, check = condition
    return criterion, field, required, check
