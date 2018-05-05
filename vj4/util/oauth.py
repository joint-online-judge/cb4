from oauthlib import oauth2
from vj4.util import options
from urllib import request, parse
import json

authorize_url = 'https://jaccount.sjtu.edu.cn/oauth2/authorize'
access_token_url = 'https://jaccount.sjtu.edu.cn/oauth2/token'
logout_url = 'https://jaccount.sjtu.edu.cn/oauth2/logout'
profile_url = 'https://api.sjtu.edu.cn/v1/me/profile'

default_client = oauth2.WebApplicationClient(options.oauth_client_id)


def get_authorize_url(redirect_url):
  url, headers, body = default_client.prepare_authorization_request(authorize_url, redirect_url=redirect_url)
  return url


async def get_profile(code, redirect_url):
  client = oauth2.WebApplicationClient(options.oauth_client_id, code)
  url, headers, body = client.prepare_token_request(access_token_url, redirect_url=redirect_url,
                                                    client_secret=options.oauth_client_secret)
  body = body.encode(encoding='utf-8')
  req = request.Request(url, body, headers)
  res = request.urlopen(req)
  data = json.loads(res.read())
  client.access_token = data['access_token']

  url, headers, body = client.add_token(profile_url)
  req = request.Request(url, body, headers)
  res = request.urlopen(req)
  data = json.loads(res.read())
  if data['errno'] == 0:
    return data['entities'][0]
  return None

def get_logout_url(redirect_url):
  return logout_url + '?' + parse.urlencode({'post_logout_redirect_uri': redirect_url})
