"""
In order to run these tests, you must specify the
PTHELMA_TEST_ENHYDRIS_API variable to contain a json-formatted string
of parameters, like this:

    { "base_url": "http://localhost:8001/",
      "user": "admin",
      "password": "topsecret",
      "station_id": 1334,
      "variable_id": 1,
      "unit_of_measurement_id": 1,
      "time_zone_id": 1
    }

The parameters station_id, variable_id, unit_of_measurement_id and
time_zone_id are used when test timeseries are created.

Note that some other testing modules may also use
PTHELMA_TEST_ENHYDRIS_API, but might require additional parameters.

Don't use a production database for that; some testing functionality
may write to the database. Although things are normally cleaned up
(e.g. test timeseries created are deleted), id serial numbers will be
affected and things might not be cleaned up if there is an error.
"""

import json
import os
from StringIO import StringIO
import textwrap
from unittest import TestCase, skipUnless

import requests

from pthelma import enhydris_api
from pthelma.timeseries import Timeseries


@skipUnless(os.getenv('PTHELMA_TEST_ENHYDRIS_API'),
            'set PTHELMA_TEST_ENHYDRIS_API')
class LoginTestCase(TestCase):

    def test_login(self):
        v = json.loads(os.getenv('PTHELMA_TEST_ENHYDRIS_API'))
        base_url, user, password = v['base_url'], v['user'], v['password']

        # Verify we are logged out
        r = requests.get(base_url)
        self.assertEquals(r.status_code, 200)
        self.assertTrue('Login' in r.text)
        self.assertFalse('Logout' in r.text)

        # Now login and verify we're logged on
        cookies = enhydris_api.login(base_url, user, password)
        r = requests.get(base_url, cookies=cookies)
        self.assertEquals(r.status_code, 200)
        self.assertFalse('Login' in r.text)
        self.assertTrue('Logout' in r.text)


def get_after_blank_line(s):
    r"""
    This helper function returns all content of s that follows the
    first empty line; it also removes \r from line endings.
    """
    lines = s.splitlines(True)
    i = 0
    while True:
        if not lines[i].strip():
            break
        i += 1
    lines = lines[i + 1:]
    return ''.join([x.replace('\r', '') for x in lines])


@skipUnless(os.getenv('PTHELMA_TEST_ENHYDRIS_API'),
            'set PTHELMA_TEST_ENHYDRIS_API')
class PostTsDataTestCase(TestCase):
    test_timeseries = textwrap.dedent("""\
                                      2014-01-01 08:00,11,
                                      2014-01-02 08:00,12,
                                      2014-01-03 08:00,13,
                                      2014-01-04 08:00,14,
                                      2014-01-05 08:00,15,
                                      """)
    test_timeseries_top = ''.join(test_timeseries.splitlines(True)[:-1])
    test_timeseries_bottom = ''.join(test_timeseries.splitlines(True)[-1])

    def test_post_tsdata(self):
        v = json.loads(os.getenv('PTHELMA_TEST_ENHYDRIS_API'))
        cookies = enhydris_api.login(v['base_url'], v['user'], v['password'])

        # Create a time series in the database
        j = {
            'gentity': v['station_id'],
            'variable': v['variable_id'],
            'unit_of_measurement': v['unit_of_measurement_id'],
            'time_zone': v['time_zone_id'],
        }
        ts_id = enhydris_api.post_model(v['base_url'], cookies, 'Timeseries',
                                        j)

        # Now upload some data
        ts = Timeseries(ts_id)
        ts.read(StringIO(self.test_timeseries_top))
        enhydris_api.post_tsdata(v['base_url'], cookies, ts)

        # Read and check the time series
        url = enhydris_api.urljoin(v['base_url'],
                                   'timeseries/d/{}/download/'.format(ts.id))
        r = requests.get(url, cookies=cookies)
        r.raise_for_status()
        self.assertEquals(get_after_blank_line(r.content),
                          self.test_timeseries_top)

        # Upload more data
        ts = Timeseries(ts_id)
        ts.read(StringIO(self.test_timeseries_bottom))
        enhydris_api.post_tsdata(v['base_url'], cookies, ts)

        # Read and check the time series
        url = enhydris_api.urljoin(v['base_url'],
                                   'timeseries/d/{}/download/'.format(ts.id))
        r = requests.get(url, cookies=cookies)
        r.raise_for_status()
        self.assertEquals(get_after_blank_line(r.content),
                          self.test_timeseries)