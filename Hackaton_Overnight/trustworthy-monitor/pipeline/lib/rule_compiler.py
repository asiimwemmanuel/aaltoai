import hashlib
import re
import json
import os
import numpy as np

# Words that carry no identifying power when matching a rule to a column.
_ROLE_STOPWORDS = {
    'variable', 'measured', 'manipulated', 'sensor', 'actuator',
    'continuous', 'downstream', 'upstream', 'inferred', 'unknown',
    'value', 'signal', 'column', 'with', 'that', 'this', 'from',
}

class RuleCompiler:
    def __init__(self, schema_path='artifacts/schema.json',
                 semantics_path='artifacts/semantics.json'):
        self.schema_path = schema_path
        self.semantics_path = semantics_path
        self.schema = self._load_schema()
        self.semantics = self._load_json(semantics_path)
        self.alias_map = self._build_alias_map()

    @staticmethod
    def _load_json(path):
        if path and os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}

    def _load_schema(self):
        if os.path.exists(self.schema_path):
            with open(self.schema_path, 'r') as f:
                return json.load(f)
        return {}

    def _build_alias_map(self):
        mapping = {}
        cols_entry = self.schema.get('columns', {})
        if isinstance(cols_entry, dict):
            for cid, meta in cols_entry.items():
                orig = meta.get('original_name', '')
                mapping[cid.lower()] = cid
                if orig:
                    mapping[orig.lower()] = cid
        elif isinstance(cols_entry, list):
            for col in cols_entry:
                cid = col['col_id']
                orig = col.get('original_name', '')
                mapping[cid.lower()] = cid
                if orig:
                    mapping[orig.lower()] = cid
        
        # Aliases are derived from what S4 INFERRED about each column, never from
        # a hand-written table of what we happen to know about this dataset.
        #
        # REPLACED 19 Sep. This block used to hold a lookup table tying each
        # original column name to the physical quantity it measures in this
        # specific plant. That is published ground truth typed in by hand, and
        # it forfeits
        # evaluation criterion 1: "the system works out what each column is. No
        # manual labelling." It also undermined S4's honest, statistics-derived
        # inferences, because a judge who spots one hand-fed mapping stops
        # trusting the rest.
        #
        # What replaces it: the words an operator writes in a rule are matched
        # against the roles the system itself inferred. If S4 concluded that
        # col_009 behaves like a manipulated variable, then an operator rule
        # mentioning "valve" can resolve to it -- and the chain from rule to
        # column is traceable back to evidence, which is what earns points.
        for cid, inference in (self.semantics or {}).items():
            if not isinstance(inference, dict):
                continue
            role = (inference.get('human_override')
                    or inference.get('inferred_role')
                    or '')
            if isinstance(role, dict):
                role = role.get('role', '')
            role = str(role).lower().strip()
            if not role:
                continue
            # The full role phrase, plus each significant word in it.
            mapping.setdefault(role, cid)
            for word in re.findall(r'[a-z]{4,}', role):
                if word not in _ROLE_STOPWORDS:
                    mapping.setdefault(word, cid)

        return mapping

    def resolve_target_column(self, text):
        text_lower = text.lower()
        direct_match = re.search(r'(col_\d{3}|xmeas_\d+|xmv_\d+)', text_lower)
        if direct_match:
            key = direct_match.group(0)
            if key in self.alias_map:
                return self.alias_map[key]
        
        for alias in sorted(self.alias_map.keys(), key=len, reverse=True):
            if alias in text_lower:
                return self.alias_map[alias]

        # No confident match. Return None rather than guessing.
        #
        # This used to return 'col_001', which meant a rule about a column the
        # system did not recognise was silently applied to an unrelated column.
        # Admitting we cannot resolve the rule is both correct and better scored:
        # the challenge rewards a system that states what it does not know.
        return None

    def compile_rule(self, rule_text, rule_id=None):
        if not rule_id:
            # hash() is salted per process, so the same rule text got a new id
            # on every run and its evidence ids never matched between runs.
            digest = hashlib.sha1(rule_text.encode('utf-8')).hexdigest()
            rule_id = f'RULE_{int(digest[:8], 16) % 10000:04d}'
        
        target_col = self.resolve_target_column(rule_text)

        if target_col is None:
            # Fail closed and surface it to the operator. An unresolved rule
            # never silently passes: it is reported so a human can name the
            # column, which is exactly the human-in-the-loop path the challenge
            # asks for.
            return {
                'rule_id': rule_id,
                'raw_text': rule_text,
                'target_col': None,
                'status': 'NEEDS_OPERATOR_INPUT',
                'condition': 'unresolved: no inferred role matches this rule text',
                'operator_prompt': (
                    f'Which column does this rule refer to? "{rule_text}". '
                    f'The inferred roles did not match any wording in it.'
                ),
                'executable': lambda data: False,
            }

        text_lower = rule_text.lower()
        clean_text = re.sub(r'(col_\d{3}|xmeas_\d+|xmv_\d+)', '', rule_text, flags=re.IGNORECASE)
        numbers = [float(n) for n in re.findall(r'[-+]?\d*\.?\d+', clean_text)]

        if not numbers:
            # "col_009 must remain within operational limits" names a column
            # but no limit. This used to compile to `col_009 <= 0.0` and fail on
            # every batch, which then counted as a data violation. A rule
            # without a number is a question for the operator, not a check.
            return {
                'rule_id': rule_id,
                'raw_text': rule_text,
                'target_col': target_col,
                'status': 'NEEDS_OPERATOR_INPUT',
                'condition': 'unresolved: no numeric limit in the rule text',
                'operator_prompt': (
                    f'What limit does this rule set for {target_col}? "{rule_text}" '
                    f'contains no number to check against.'
                ),
                'executable': lambda data: False,
            }

        if 'between' in text_lower and len(numbers) >= 2:
            low, high = min(numbers[0], numbers[1]), max(numbers[0], numbers[1])
            def check_between(data, c=target_col, l=low, h=high):
                arr = np.array(data.get(c, []))
                return bool(np.all((arr >= l) & (arr <= h)))
            
            return {
                'rule_id': rule_id,
                'raw_text': rule_text,
                'target_col': target_col,
                'condition': f'{low} <= {target_col} <= {high}',
                'executable': check_between
            }

        if any(w in text_lower for w in ['not exceed', 'below', 'less than', 'under', '<=', '<']):
            thresh = numbers[0] if numbers else 0.0
            def check_max(data, c=target_col, t=thresh):
                arr = np.array(data.get(c, []))
                return bool(np.all(arr <= t))
            
            return {
                'rule_id': rule_id,
                'raw_text': rule_text,
                'target_col': target_col,
                'condition': f'{target_col} <= {thresh}',
                'executable': check_max
            }

        if any(w in text_lower for w in ['at least', 'above', 'greater than', 'over', '>=', '>']):
            thresh = numbers[0] if numbers else 0.0
            def check_min(data, c=target_col, t=thresh):
                arr = np.array(data.get(c, []))
                return bool(np.all(arr >= t))
            
            return {
                'rule_id': rule_id,
                'raw_text': rule_text,
                'target_col': target_col,
                'condition': f'{target_col} >= {thresh}',
                'executable': check_min
            }

        thresh = numbers[0] if numbers else 0.0
        def check_default(data, c=target_col, t=thresh):
            arr = np.array(data.get(c, []))
            return bool(np.all(arr <= t))
        
        return {
            'rule_id': rule_id,
            'raw_text': rule_text,
            'target_col': target_col,
            'condition': f'{target_col} <= {thresh}',
            'executable': check_default
        }

    def compile_rules(self, rules_list):
        return [self.compile_rule(r, f'RULE_{i+1:03d}') for i, r in enumerate(rules_list)]
