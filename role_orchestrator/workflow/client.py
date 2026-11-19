import json
import socket
import uuid
from ..contracts import strict_json

class ControlError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

class Client:
    def __init__(self, path, run_id):
        self.path, self.run_id = str(path), run_id

    def call(self, method, **params):
        request_id = uuid.uuid4().hex
        request = {'version': 1, 'request_id': request_id, 'run_id': self.run_id,
                   'method': method, 'params': params}
        data = (json.dumps(request, separators=(',', ':'))+'\n').encode()
        if len(data)>64*1024:
            raise ValueError('control request exceeds limit')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(6)
            sock.connect(self.path)
            sock.sendall(data)
            with sock.makefile('rb') as stream:
                response = stream.readline(256*1024+1)
        if len(response)>256*1024 or not response.endswith(b'\n'):
            raise ValueError('invalid control response frame')
        result = strict_json(response)
        if result.get('error', {}).get('code') == 'busy':
            raise ControlError('busy')
        if (type(result.get('version')) is not int or result['version'] != 1
            or result.get('request_id') != request_id or result.get('run_id') != self.run_id
            or type(result.get('ok')) is not bool):
            raise ValueError('control response identity mismatch')
        if not result['ok']:
            raise ControlError(result['error']['code'])
        return result['result']
