"""Exercise the running demo's HTTP boundaries without provider calls."""
import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:4111')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    def status():
        with urlopen(args.url+'/api/status', timeout=15) as response:
            return json.load(response)
    before = status()
    valid = {'Origin':args.url,'Content-Type':'application/json','X-Demo-Token':before['csrf_token']}
    cases = []
    tests = [
        ('origin', {'query':'must not be submitted'}, {'Origin':'https://external.invalid'},403),
        ('token', {'query':'must not be submitted'}, {'X-Demo-Token':'invalid'},403),
        ('blank', {'query':'   '}, {},400),
        ('model_override', {'query':'test','model':'unapproved'}, {},400),
        ('invalid_refresh', {'query':'test','refresh':'true'}, {},400),
        ('malformed_json', b'{', {},400),
        ('oversized', b'x'*8193, {},413),
        ('content_type', {'query':'test'}, {'Content-Type':'text/plain'},415),
    ]
    for name, body, override, expected in tests:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = Request(args.url+'/api/query', data=data, headers={**valid,**override})
        try:
            with urlopen(request, timeout=15) as response:
                actual=response.status
        except HTTPError as error:
            actual=error.code
            json.load(error)
        assert actual==expected, (name,actual,expected)
        cases.append({'id':name,'status':'passed','http_status':actual})
    after=status()
    assert after['counts']==before['counts'], 'Invalid requests reached a provider or completed a query'
    assert after['row_digest']==before['row_digest'], 'Invalid requests changed Oracle data'
    output={'status':'passed','cases':cases,'counts_before':before['counts'],'counts_after':after['counts'],
            'database_rows_unchanged':True,'run_id':after['run_id']}
    path=Path(args.output)
    path.write_text(json.dumps(output,indent=2)+'\n')
    path.chmod(0o600)
    print(json.dumps({'status':'passed','http_controls':len(cases),'provider_calls_from_invalid_requests':0}))


if __name__=='__main__':
    main()
