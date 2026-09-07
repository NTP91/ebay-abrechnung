import unittest

import lexware_config


class LexwareConfigTests(unittest.TestCase):
    def test_prefers_streamlit_secret_without_exposing_it(self):
        key,source=lexware_config.configured_api_key({'lexware':{'api_key':' secret '}},{'LEXWARE_API_KEY':'environment'})
        self.assertEqual(key,'secret')
        self.assertEqual(source,'st.secrets["lexware"]["api_key"]')

    def test_supports_legacy_lexoffice_section_and_environment_fallback(self):
        self.assertEqual(lexware_config.configured_api_key({'lexoffice':{'token':'legacy'}},{}),('legacy','st.secrets["lexoffice"]["token"]'))
        self.assertEqual(lexware_config.configured_api_key({}, {'LEXOFFICE_API_KEY':'environment'}),('environment','LEXOFFICE_API_KEY'))

    def test_missing_or_blank_values_return_empty(self):
        self.assertEqual(lexware_config.configured_api_key({'lexware':{'api_key':'  '}},{}),('',''))


if __name__=='__main__':unittest.main()
