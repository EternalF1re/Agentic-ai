import runpy, json

mod = runpy.run_path('./workspace/02-the-agent-loop-async.py')
fn = mod.get('get_current_weather_sync')
print('fn exists?', fn is not None)
if fn:
    res = fn('Chengdu','celsius')
    print('res:', json.dumps(res, ensure_ascii=False))
else:
    print('function missing')
