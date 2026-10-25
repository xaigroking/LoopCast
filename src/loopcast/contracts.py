"""Public component contract; declarations are distinct from runtime evidence."""
import re

MPT_REVISION = '82bb4a7502704559575f4e3d002b9dcb31d4ed6a'
MPT_SOURCE = 'https://github.com/harry0703/MoneyPrinterTurbo'
CAPABILITIES = {'generate', 'publish', 'query', 'schedule', 'ai_disclosure'}


def manifest(value):
    if not isinstance(value, dict) or set(value) != {'source', 'revision', 'artifact_sha256', 'contract_major', 'capabilities', 'verification'}:
        raise ValueError('Invalid component manifest')
    if value['source'] != MPT_SOURCE or value['revision'] != MPT_REVISION:
        raise ValueError('Unsupported component revision')
    if type(value['contract_major']) is not int or value['contract_major'] != 1:
        raise ValueError('Unsupported component contract')
    if not isinstance(value['artifact_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['artifact_sha256']):
        raise ValueError('Component artifact must be locked')
    if value['verification'] not in {'unverified', 'contract_tested'}:
        raise ValueError('Unsupported verification evidence')
    if not isinstance(value['capabilities'], dict) or set(value['capabilities']) != CAPABILITIES:
        raise ValueError('Unknown component capabilities')
    if value['capabilities'] != {k: k == 'generate' for k in CAPABILITIES}:
        raise ValueError('Only generation is enabled by this contract')
    return value.copy()
