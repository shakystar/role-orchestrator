import copy
import unittest
from role_orchestrator.contracts import validate_request, validate_response, strict_json


def request():
    return {'schema_version':1,'run_id':'run-1','attempt_id':'attempt-1','id':'task-1','role':'reviewer','instruction':'Review','input':'{}','traceparent':'00-'+'1'*32+'-'+'2'*16+'-03'}


class ContractTests(unittest.TestCase):
    def test_request_constraints(self):
        original = request()
        validate_request(original)  # Python OTel random-trace flag 03 is valid W3C.
        changes = [('schema_version', True), ('schema_version', 2), ('id', '.invalid'), ('role', 'a'*65), ('run_id', None), ('instruction', ''), ('input', 'a'*65537), ('traceparent', '00-'+'0'*32+'-'+'2'*16+'-01')]
        for field,value in changes:
            with self.subTest(field=field,value=str(value)[:20]):
                changed={**original, field:value}
                with self.assertRaises(ValueError): validate_request(changed)
        for field in original:
            changed=dict(original);changed.pop(field)
            with self.assertRaises(ValueError): validate_request(changed)
        with self.assertRaises(ValueError):validate_request({**original,'extra':1})
        with self.assertRaises(ValueError):strict_json('{"id":"a","id":"b"}')

    def test_response_identity_and_cache_accounting(self):
        req=request()
        response={'schema_version':1,'run_id':req['run_id'],'attempt_id':req['attempt_id'],'id':req['id'],'status':'succeeded','text':'ok','cached':True,'stats':{k:0 for k in ['submitted','succeeded','failed','rejected','maxActive','maxQueued','workers']}}
        validate_response(response,req)
        for field,value in [('schema_version',True),('attempt_id','other'),('cached',1),('text',None),('extra','unexpected')]:
            with self.assertRaises(ValueError):validate_response({**response,field:value},req)
        changed=copy.deepcopy(response);changed['stats']['submitted']=1
        with self.assertRaises(ValueError):validate_response(changed,req)
        changed=copy.deepcopy(response);changed['stats']['workers']=False
        with self.assertRaises(ValueError):validate_response(changed,req)

    def test_shared_vectors(self):
        import json
        from pathlib import Path
        cases = json.loads((Path(__file__).resolve().parents[1]/'contracts/v1-vectors.json').read_text())
        for case in cases:
            with self.subTest(case=case['name']):
                if case['valid']:
                    validate_request(strict_json(case['wire']))
                else:
                    with self.assertRaises(ValueError):
                        validate_request(strict_json(case['wire']))
