from bson import objectid
from os import path, makedirs
from shutil import rmtree
from tempfile import mkdtemp
from mosspy import Moss
from io import BytesIO

import rarfile, tarfile, zipfile

from vj4 import constant
from vj4 import db
from vj4 import error
from vj4.model import fs
from vj4.util import options


# @TODO(tc-imba) the extraction is not safe now.

def extract_text_file(file_obj, dest_dir, lang):
    wildcards = constant.language.LANG_MOSS_WILDCARDS.get(lang, [])
    if wildcards:
        name = wildcards[0].replace('*', 'main')
    else:
        name = 'main.txt'
    with open(path.join(dest_dir, name), mode='wb') as file:
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


async def moss_test(rdocs: list, language: str, ignore_limit: int = 10, wildcards: list = None):
    # check the language is supported by moss
    if language not in constant.language.LANG_MOSS:
        raise error.LanguageNotSupportedError(language)

    ignore_limit = int(ignore_limit)
    if ignore_limit < 2:
        raise error.InvalidArgumentError('ignore_limit')

    if wildcards is None or len(wildcards) == 0:
        wildcards = constant.language.LANG_MOSS_WILDCARDS.get(language, [])

    moss_dir = mkdtemp(prefix='cb4.moss.')
    try:
        moss = Moss(options.moss_user_id, language)
        moss.setDirectoryMode(1)
        moss.setIgnoreLimit(ignore_limit)

        for rdoc in rdocs:
            if rdoc['code_type'] in EXTRACT_OPEN_FUNC:
                try:
                    grid_out = await fs.get(rdoc['code'])
                    file_obj = BytesIO(await grid_out.read())
                    dest_dir = path.join(moss_dir, str(rdoc['pid']), str(rdoc['uid']))
                    makedirs(dest_dir, exist_ok=True)
                    EXTRACT_OPEN_FUNC[rdoc['code_type']](file_obj, dest_dir, language)
                    for wildcard in wildcards:
                        moss.addFilesByWildcard(path.join(dest_dir, wildcard))
                except Exception as e:
                    print(e)

        url = moss.send()
        return url

    finally:
        # print(moss_dir)
        rmtree(moss_dir, ignore_errors=True)
