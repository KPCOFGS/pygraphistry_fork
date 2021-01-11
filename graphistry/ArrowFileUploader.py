from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .arrow_uploader import ArrowUploader

import logging, pyarrow as pa, requests
from functools import lru_cache
from weakref import WeakKeyDictionary

logger = logging.getLogger('ArrowFileUploader')


# WrappedTable -> {'file_id': str, 'output': dict}
DF_TO_FILE_ID_CACHE : WeakKeyDictionary = WeakKeyDictionary()
"""
NOTE: Will switch to pa.Table -> ... when RAPIDS upgrades from pyarrow, 
     which adds weakref support
"""

class ArrowFileUploader():
    """
        Implement file API with focus on Arrow support

        Memoization in this class is based on reference equality, while plotter is based on hash.
        That means the plotter resolves different-identity value matches, so by the time ArrowFileUploader compares,
        identities are unified for faster reference-based checks.

        Example: Upload files with per-session memoization
            uploader : ArrowUploader
            arr : pa.Table
            afu = ArrowFileUploader(uploader)

            file1_id = afu.create_and_post_file(arr)[0]
            file2_id = afu.create_and_post_file(arr)[0]

            assert file1_id == file2_id # memoizes by default (memory-safe: weak refs)

        Example: Explicitly create a file and upload data for it
            uploader : ArrowUploader
            arr : pa.Table
            afu = ArrowFileUploader(uploader)

            file1_id = afu.create_file()
            afu.post_arrow(arr, file_id)

            file2_id = afu.create_file()
            afu.post_arrow(arr, file_id)

            assert file1_id != file2_id

    """

    uploader: 'ArrowUploader'

    def __init__(self, uploader: 'ArrowUploader'):
        self.uploader = uploader

    ###

    def create_file(self, file_opts: dict = {}) -> str:
        """
            Creates File and returns file_id str.
            
            Defauls:
              - file_type: 'arrow'

            See File REST API for file_opts

        """

        tok = self.uploader.token

        json_extended = {
            'file_type': 'arrow',
            **file_opts
        }

        res = requests.post(
            self.uploader.server_base_path + '/api/v2/files/',
            verify=self.uploader.certificate_validation,
            headers={'Authorization': f'Bearer {tok}'},
            json=json_extended)

        try:            
            out = res.json()
            logger.debug('Server create file response: %s', out)
            if (not 'success' in out) or (not out['success']):
                raise Exception(out)
        except Exception as e:
            logger.error('Failed creating file: %s', res.text, exc_info=True)
            raise e
        
        self.dataset_id = out['data']['file_id']

        return out

    def post_arrow(self, arr: pa.Table, file_id: str, url_opts: str = 'erase=true') -> dict:
        """
            Upload new data to existing file id

            Default url_opts='erase=true' throws exceptions on parse errors and deletes upload.

            See File REST API for url_opts (file upload)
        """

        buf = self.uploader.arrow_to_buffer(arr)

        tok = self.uploader.token
        base_path = self.uploader.server_base_path

        url = f'{base_path}/api/v2/upload/files/{file_id}'
        if len(url_opts) > 0:
            url = f'{url}?{url_opts}'

        out = requests.post(
            url,
            verify=self.uploader.certificate_validation,
            headers={'Authorization': f'Bearer {tok}'},
            data=buf).json()
        
        if not out['success']:
            raise Exception(out)
            
        return out

    ###

    def create_and_post_file(self, arr: pa.Table, file_id: str = None, file_opts: dict = {}, upload_url_opts: str = 'erase=true', memoize: bool = True) -> (str, dict):
        """
            Create file and upload data for it.

            Default upload_url_opts='erase=true' throws exceptions on parse errors and deletes upload.

            Default memoize=True skips uploading 'arr' when previously uploaded in current session

            See File REST API for file_opts (file create) and upload_url_opts (file upload)
        """

        logger.warning('@create_and_post_file')
        logger.warning('items: %s', [x for x in DF_TO_FILE_ID_CACHE.items()])

        if memoize:
            #FIXME if pa.Table was hashable, could do direct set/get map
            for wrapped_table, val in DF_TO_FILE_ID_CACHE.items():
                logger.warning('Checking: %s', wrapped_table)
                if wrapped_table.arr is arr:
                    return val.file_id, val.output

        if file_id is None:
            file_id = self.create_file(file_opts)
        
        resp = self.post_arrow(arr, file_id, upload_url_opts)
        out = MemoizedFileUpload(file_id, resp)

        if memoize:
            wrapped = WrappedTable(out)
            cache_arr(wrapped)
            DF_TO_FILE_ID_CACHE[wrapped] = MemoizedFileUpload(file_id, out)
            logger.debug('Memoized file %s', file_id)
        
        return out.file_id, out.output

@lru_cache(maxsize=100)
def cache_arr(arr):
    """
        Hold reference to most recent memoization entries
        Hack until RAPIDS supports Arrow 2.0, when pa.Table becomes weakly referenceable
    """
    return arr

class WrappedTable():
    arr : pa.Table
    def __init__(self, arr: pa.Table):
        self.arr = arr

class MemoizedFileUpload():    
    file_id: str
    output: dict
    def __init__(self, file_id: str, output: dict):
        self.file_id = file_id
        self.output = output