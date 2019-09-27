from os import path, mkdir
from shutil import rmtree
from tempfile import mkdtemp
from mosspy import Moss
from io import BytesIO

import rarfile, tarfile, zipfile

from vj4 import constant
from vj4 import db
from vj4 import error
from vj4.model import fs


# @TODO(tc-imba) the extraction is not safe now.

def extract_text_file(file_obj, dest_dir, lang):
  # @TODO add extension
  with open(path.join(dest_dir, 'main' + '.cpp'), mode='wb') as file:
    file.write(file_obj.read())


def extract_tar_file(file_obj, dest_dir, lang):
  with tarfile.open(fileobj=file_obj) as file:
    file.extractall(path=dest_dir)


def extract_zip_file(file_obj, dest_dir, lang):
  with zipfile.ZipFile(file_obj) as file:
    file.extractall(path=dest_dir)


def extract_rar_file(file_obj, dest_dir, lang):
  with rarfile.RarFile(file_obj) as file:
    file.extractall(path=dest_dir)


EXTRACT_OPEN_FUNC = {
  constant.record.FILE_TYPE_TEXT: extract_text_file,
  constant.record.FILE_TYPE_TAR: extract_tar_file,
  constant.record.FILE_TYPE_ZIP: extract_zip_file,
  constant.record.FILE_TYPE_RAR: extract_rar_file
}


async def moss_test(rdocs: list, language: str):
  # check the language is supported by moss
  if language not in constant.language.LANG_MOSS:
    raise error.LanguageNotSupportedError(language)
  
  moss_dir = mkdtemp(prefix='cb4.moss.')
  try:
    for rdoc in rdocs:
      grid_out = await fs.get(rdoc['code'])
      file_obj = BytesIO(await grid_out.read())
      dest_dir = path.join(moss_dir, str(rdoc['uid']))
      mkdir(dest_dir)
      if rdoc['code_type'] in EXTRACT_OPEN_FUNC:
        EXTRACT_OPEN_FUNC[rdoc['code_type']](file_obj, dest_dir, language)
      
  finally:
    print(moss_dir)
    rmtree(moss_dir, ignore_errors=True)
  
