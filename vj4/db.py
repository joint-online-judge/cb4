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
  while True:
    try:
      _client = await aiomongo.create_client('mongodb://' + options.db_host)
    except OSError as e:
      _logger.error(e.args)
      _logger.error('Unable to connect mongodb, try again 5 seconds later')
      time.sleep(5)
    else:
      break
  _db = _client.get_database(options.db_name)


@functools.lru_cache()
def coll(name):
  return aiomongo.Collection(_db, name)


@functools.lru_cache()
def fs(name):
  return aiomongo.GridFS(_db, name)
