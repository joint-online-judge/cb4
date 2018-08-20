import aiomongo
import functools
import logging
import time

from vj4.util import options

options.define('db_host', default='localhost', help='Database hostname or IP address.')
options.define('db_name', default='test', help='Database name.')

_logger = logging.getLogger(__name__)

async def init():
  global _client, _db
  error_count = 0
  while True:
    try:
      _client = await aiomongo.create_client('mongodb://' + options.db_host)
    except OSError as e:
      if error_count < 10:
        error_count += 1
        _logger.error(e.args)
        _logger.error('Unable to connect mongodb, try again 5 seconds later (%d/10)' % error_count)
        time.sleep(5)
      else:
        raise
    else:
      break
  _db = _client.get_database(options.db_name)


@functools.lru_cache()
def coll(name):
  return aiomongo.Collection(_db, name)


@functools.lru_cache()
def fs(name):
  return aiomongo.GridFS(_db, name)
