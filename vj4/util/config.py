import yaml
import sys


class obj(object):
  def __init__(self, d):
    for a, b in d.items():
      if isinstance(b, (list, tuple)):
        setattr(self, a, [obj(x) if isinstance(x, dict) else x for x in b])
      else:
        setattr(self, a, obj(b) if isinstance(b, dict) else b)


class Config:

  def __init__(self):
    try:
      f = open('config.yaml')
      dic = yaml.safe_load(f)
      self._namespace = obj(dic)
      f.close()
    except FileNotFoundError:
      print('config.yaml not found')
      exit(-1)

  def __getattr__(self, item):
    return getattr(self._namespace, item)


sys.modules[__name__] = Config()
