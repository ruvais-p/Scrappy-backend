"""Small reproducible read-only load probe for staging measurements.

Usage: python tools/load_probe.py --base-url http://127.0.0.1:8080 --tabs 500
The probe intentionally records observations; it does not assert capacity.
"""
import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--tabs',type=int,default=500)
    parser.add_argument('--actions',type=int,default=25)
    parser.add_argument('--output',default='load-probe.json')
    args=parser.parse_args()
    paths=['api/health/live/','api/health/ready/','api/v1/options/','api/v1/dashboard/','api/v1/leads/?page=1']
    paths=(paths*((args.actions+len(paths)-1)//len(paths)))[:args.actions]
    jobs=[paths[i%len(paths)] for i in range(args.tabs)]
    def request(path):
        started=time.perf_counter()
        try:
            with urlopen(Request(args.base_url.rstrip('/')+'/'+path),timeout=15) as response:
                response.read(256);status=response.status
        except Exception as error:
            status='error:'+type(error).__name__
        return {'path':path,'status':status,'milliseconds':round((time.perf_counter()-started)*1000,2)}
    started=time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(args.tabs,100)) as pool: results=list(pool.map(request,jobs))
    durations=[r['milliseconds'] for r in results]
    report={'base_url':args.base_url,'tabs':args.tabs,'actions':args.actions,'elapsed_seconds':round(time.perf_counter()-started,3),'successful':sum(isinstance(r['status'],int) and r['status']<400 for r in results),'p50_ms':statistics.median(durations),'max_ms':max(durations),'results':results}
    with open(args.output,'w',encoding='utf-8') as output: json.dump(report,output,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k!='results'},indent=2))


if __name__=='__main__': main()
