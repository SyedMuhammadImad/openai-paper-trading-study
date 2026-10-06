"""Run an offline demo or replay a local OHLC export. All executions remain paper-only."""
import argparse,json
from pathlib import Path
from paper import PaperAccount,read_bars,demo_bars,demo_signal,replay
from providers import Provider
def main(kind,min_rr):
    parser=argparse.ArgumentParser(description='Standalone '+kind+' paper trading study; no live orders')
    parser.add_argument('--data',type=Path,help='Local MT5 or other OHLC export; see README schema')
    parser.add_argument('--provider',choices=['demo',kind],default='demo')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--capital',type=float,default=10000.)
    parser.add_argument('--point-value',type=float,default=1.,help='Account currency per full price unit per position unit')
    parser.add_argument('--unit-step',type=float,default=.01)
    parser.add_argument('--margin-per-unit',type=float,default=1.)
    parser.add_argument('--fee-per-unit',type=float,default=0.,help='Round-trip fee, charged at paper entry')
    args=parser.parse_args()
    try:
        if args.provider!='demo' and args.data is None:raise ValueError('Provider use requires an explicit local data file')
        bars=read_bars(args.data) if args.data else demo_bars()
        account=PaperAccount(capital=args.capital,min_rr=min_rr,point_value=args.point_value,unit_step=args.unit_step,margin_per_unit=args.margin_per_unit,fee_per_unit=args.fee_per_unit)
        provider=Provider(kind) if args.provider!='demo' else lambda context:demo_signal(context,min_rr)
        result=replay(bars,provider,account)
        if args.output:
            with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2,allow_nan=False)
        else:print(json.dumps(result,indent=2,allow_nan=False))
    except (ValueError,RuntimeError,OSError) as error:parser.exit(2,str(error)+'\n')
