from oauthlib import oauth2
from vj4.util import config
from urllib import request, parse

authorize_url = 'https://jaccount.sjtu.edu.cn/oauth2/authorize'
access_token_url = 'https://jaccount.sjtu.edu.cn/oauth2/token'
profile_url = 'https://api.sjtu.edu.cn/v1/me/profile'

default_client = oauth2.WebApplicationClient(config.jaccount.client_id)


def get_authorize_url(redirect_url):
  url, headers, body = default_client.prepare_authorization_request(authorize_url, redirect_url=redirect_url)
  return url


async def get_profile(code, redirect_url):
  client = oauth2.WebApplicationClient(config.jaccount.client_id, code)
  url, headers, body = client.prepare_token_request(access_token_url, redirect_url=redirect_url,
                                                    client_secret=config.jaccount.client_secret)
  print(url)
  print(headers)
  print(body)
  req = request.Request(url, body, headers)
  res = request.urlopen(req)
  res = res.read()
  print(res)
