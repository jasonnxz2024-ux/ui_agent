"""Source-owned types and execution requirements, independent of LVGL.

Parsing a rule is not evidence that a backend implements it. This pass retains
lexical ownership and checks bounded data before the legacy backend is invoked.
"""
from __future__ import annotations

import math
from typing import Any


class RuleError(ValueError):
    pass


def constant(expression: Any) -> Any:
    """Evaluate literals only; distinguish unknown syntax from a literal null."""
    if not isinstance(expression, list) or not expression:
        raise RuleError('initializer is not a literal expression')
    kind = expression[0]
    if kind == 'literal':
        return expression[1]
    if kind == 'array':
        return [constant(value) for value in expression[1]]
    if kind == 'object':
        result = {}
        for key, value in expression[1]:
            if key == 'spread':
                raise RuleError('initializer spread needs constant evaluation')
            name = constant(key)
            if not isinstance(name, str):
                raise RuleError('record field must have a string name')
            result[name] = constant(value)
        return result
    if kind == 'unary' and expression[1] in {'-', '+'}:
        value = constant(expression[2])
        if type(value) not in (int, float):
            raise RuleError('numeric initializer expected')
        return -value if expression[1] == '-' else value
    raise RuleError(f'initializer requires lowering: {kind}')


def infer_type(value: Any) -> dict:
    if value is None:
        return {'kind': 'null'}
    if type(value) is bool:
        return {'kind': 'boolean'}
    if type(value) in (int, float):
        return {'kind': 'number'}
    if isinstance(value, str):
        return {'kind': 'string'}
    if isinstance(value, dict):
        return {'kind': 'record', 'fields': {k: infer_type(v) for k, v in value.items()}}
    if isinstance(value, list):
        if not value:
            raise RuleError('empty list requires an explicit element type')
        element = infer_type(value[0])
        if any(infer_type(item) != element for item in value[1:]):
            raise RuleError('heterogeneous list requires an explicit union type')
        return {'kind': 'list', 'element': element}
    raise RuleError('unknown value type')


def resolve_type(schema: dict, definitions: dict, file: str, capacity: int,
                 stack: tuple[str, ...] = ()) -> dict:
    kind = schema.get('kind', 'unknown')
    if kind == 'reference':
        key = file + '#' + schema['name']
        if key in stack:
            raise RuleError(f'recursive data type is not bounded: {key}')
        if key not in definitions:
            raise RuleError(f'unresolved data type: {key}')
        resolved = resolve_type(definitions[key], definitions, file, capacity, (*stack, key))
        return {**resolved, **({'optional': True} if schema.get('optional') else {})}
    if kind == 'unknown':
        raise RuleError(f'unsupported declared type: {schema.get("source", "unknown")}')
    result = {k: v for k, v in schema.items() if k != 'source'}
    if kind == 'list':
        if not schema.get('element'):
            raise RuleError('list element type is missing')
        result.update(capacity=capacity, element=resolve_type(schema['element'], definitions, file, capacity, stack))
    elif kind == 'record':
        result['fields'] = {name: resolve_type(field, definitions, file, capacity, stack)
                            for name, field in schema['fields'].items()}
    elif kind == 'union':
        result['members'] = [resolve_type(member, definitions, file, capacity, stack) for member in schema['members']]
    elif kind == 'string':
        result['capacity_bytes'] = 127  # Current target string storage, explicit.
    elif kind not in {'number', 'boolean', 'null', 'literal', 'host-file'}:
        raise RuleError(f'unknown schema kind: {kind}')
    return result


def validate_value(schema: dict, value: Any, path: str = 'value') -> None:
    """Reject the whole value on overflow/type failure; never truncate a list."""
    kind = schema['kind']
    valid = False
    if kind == 'union':
        for member in schema['members']:
            try:
                validate_value(member, value, path)
                return
            except RuleError:
                pass
    elif kind == 'record':
        if isinstance(value, dict):
            extra = value.keys() - schema['fields'].keys()
            if extra:
                raise RuleError(f'{path}: unknown fields {sorted(extra)}')
            for name, field in schema['fields'].items():
                if name not in value and field.get('optional'):
                    continue
                if name not in value:
                    raise RuleError(f'{path}.{name}: required field missing')
                validate_value(field, value[name], f'{path}.{name}')
            return
    elif kind == 'list':
        if isinstance(value, list):
            if len(value) > schema['capacity']:
                raise RuleError(f'{path}: list capacity {schema["capacity"]} exceeded ({len(value)})')
            for index, item in enumerate(value):
                validate_value(schema['element'], item, f'{path}[{index}]')
            return
    elif kind == 'string':
        if isinstance(value, str):
            if '\0' in value:
                raise RuleError(f'{path}: embedded NUL requires a length-aware target string')
            if len(value.encode('utf-8')) > schema['capacity_bytes']:
                raise RuleError(f'{path}: UTF-8 string capacity {schema["capacity_bytes"]} exceeded')
            return
    elif kind == 'number':
        valid = type(value) in (int, float) and math.isfinite(value)
    elif kind == 'boolean':
        valid = type(value) is bool
    elif kind == 'null':
        valid = value is None
    elif kind == 'literal':
        valid = type(value) is type(schema['value']) and value == schema['value']
        if valid and isinstance(value, str):
            validate_value({'kind': 'string', 'capacity_bytes': 127}, value, path)
    elif kind == 'host-file':
        raise RuleError(f'{path}: File requires a Windows host handle, not a JSON object')
    if not valid:
        raise RuleError(f'{path}: value does not match {kind}')


def compile_state_rules(contract: dict, capacity: int = 64) -> dict:
    if type(capacity) is not int or not 1 <= capacity <= 1024:
        raise RuleError('List capacity must be an integer between 1 and 1024')
    scopes = {scope['id']: scope for scope in contract.get('scopes', [])}
    result = {'schema': 'uagent.state-rules/v1', 'status': 'parsed',
              'target_execution': 'legacy-backend-gated', 'list_capacity': capacity,
              'components': [], 'tasks': contract.get('tasks', []), 'blockers': []}

    def owner(scope):
        seen = set()
        while scope and scope['kind'] != 'component':
            if scope['id'] in seen:
                raise RuleError('cyclic lexical scope')
            seen.add(scope['id'])
            scope = scopes.get(scope['parent'])
        return scope

    for scope in scopes.values():
        if scope['kind'] != 'component':
            continue
        members = [item for item in scopes.values() if owner(item) == scope]
        component = {'id': scope['id'], 'name': scope['name'], 'file': scope['file'],
                     'states': [], 'functions': [], 'events': [], 'loops': [], 'host_calls': []}
        for member in members:
            component['functions'].append({'id': member['id'], 'name': member['name'],
                                           'parent': member['parent'], 'definition': member['definition']})
            component['events'].extend(member['events'])
            component['loops'].extend(member['loops'])
            component['host_calls'].extend(call for call in member['calls']
                                           if call['callee'] in {'alert', 'confirm', 'window.alert', 'window.confirm'}
                                           or call['callee'].startswith('toast.'))
            for state in member['states']:
                item = {**state, 'id': member['id'] + '/' + state['name'], 'scope': member['id']}
                try:
                    if member is not scope:
                        raise RuleError('hook inside a nested function needs hook-order validation')
                    initial = constant(state['initial'])
                    schema = state.get('declaredType') or infer_type(initial)
                    item['type'] = resolve_type(schema, contract.get('types', {}), scope['file'], capacity)
                    validate_value(item['type'], initial, item['id'])
                    item['initial_value'] = initial
                    item['status'] = 'typed'
                except RuleError as exc:
                    item.update(status='blocked', blocker=str(exc))
                    result['blockers'].append(f'{item["id"]}: {exc}')
                component['states'].append(item)
        result['components'].append(component)
    result['summary'] = {
        'components': len(result['components']),
        'states': sum(len(c['states']) for c in result['components']),
        'typed_states': sum(s['status'] == 'typed' for c in result['components'] for s in c['states']),
        'events': sum(len(c['events']) for c in result['components']),
        'list_roots': sum(len(c['loops']) for c in result['components']),
        'tasks': len(result['tasks']),
        'host_calls': sum(len(c['host_calls']) for c in result['components']),
        'target_execution': result['target_execution'],
    }
    if result['blockers']:
        result['status'] = 'blocked'
    return result
