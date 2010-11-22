#!/usr/bin/python
"""
Timeseries processing
=====================

Copyright (C) 2005-2010 National Technical University of Athens

Copyright (C) 2005 Antonis Christofides

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""

import zlib
import random
import re
import textwrap
from datetime import datetime, timedelta
from StringIO import StringIO
from math import sin, cos, atan2, pi
from ConfigParser import ParsingError
from codecs import BOM_UTF8
from os import SEEK_CUR

import psycopg2
import fpconst

from ctypes import CDLL, c_int, c_longlong, c_double,\
                   c_char_p, byref, Structure, c_void_p

class T_REC(Structure):
    _fields_ = [("timestamp", c_longlong),
                ("null", c_int),
                ("value", c_double),
                ("flags", c_char_p)]

import platform
dickinson = CDLL('dickinson.dll' if platform.system()=='Windows'
                                                    else 'libdickinson.so')

dickinson.ts_get_item.restype = T_REC

re_compiled = re.compile(r'''^(\d{4})-(\d{1,2})-(\d{1,2})
             (?: [ tT] (\d{1,2}):(\d{1,2}) )?''',
              re.VERBOSE)

def datetime_from_iso(isostring):
    m = re_compiled.match(isostring)
    if m==None:
        raise ValueError('%s is not a valid date and time' % (isostring))
    args = []
    for x in m.groups():
        if not (x is None):
            args.append(int(x))
    return datetime(*args)

def isoformat_nosecs(adatetime, sep='T'):
    return adatetime.isoformat(sep)[:16]

class IntervalType:
    SUM = 1
    AVERAGE = 2
    MINIMUM = 3
    MAXIMUM = 4
    VECTOR_AVERAGE = 5

class TimeStep:
    def __init__(self, length_minutes=0, length_months=0, interval_type=None,
                            nominal_offset=None, actual_offset=(0,0)):
        self.length_minutes = length_minutes
        self.length_months = length_months
        self.nominal_offset = nominal_offset
        self.actual_offset = actual_offset
        self.interval_type = interval_type
    def _check_nominal_offset(self):
        """Called whenever an operation requires a nominal offset; verifies
        that the nominal offset is not None, otherwise raises exception."""
        if self.nominal_offset: return
        raise ValueError("This operation requires a nominal offset")
    def up(self, timestamp):
        self._check_nominal_offset()
        if self.length_minutes:
            required_modulo = self.nominal_offset[0]
            if required_modulo < 0: required_modulo += self.length_minutes
            reference_date = timestamp.replace(day=1, hour=0, minute=0)
            d = timestamp - reference_date
            diff_in_minutes = d.days*1440 + d.seconds/60
            actual_modulo = diff_in_minutes % self.length_minutes
            result = timestamp - timedelta(minutes=actual_modulo-required_modulo)
            while result < timestamp:
                result += timedelta(minutes=self.length_minutes)
            return result
        else:
            y = timestamp.year-1
            m = 1 + self.nominal_offset[1]
            result = timestamp.replace(year=y, month=m, day=1, hour=0, 
                minute=0) + timedelta(minutes=self.nominal_offset[0])
            while result < timestamp: 
                m += self.length_months
                if m>12:
                    m -= 12
                    y += 1
                result = timestamp.replace(year=y, month=m, day=1, hour=0,
                    minute=0) + timedelta(minutes=self.nominal_offset[0])
            return result

    def down(self, timestamp):
        self._check_nominal_offset()
        if self.length_minutes:
            required_modulo = self.nominal_offset[0]
            if required_modulo < 0: required_modulo += self.length_minutes
            reference_date = timestamp.replace(day=1, hour=0, minute=0)
            d = timestamp - reference_date
            diff_in_minutes = d.days*1440 + d.seconds/60
            actual_modulo = diff_in_minutes % self.length_minutes
            result = timestamp + timedelta(minutes=required_modulo-actual_modulo)
            while result > timestamp:
                result -= timedelta(minutes=self.length_minutes)
        elif self.length_months:
            y = timestamp.year+1
            m = 1 + self.nominal_offset[1]
            result = timestamp.replace(year=y, month=m, day=1, hour=0, 
                minute=0) + timedelta(minutes=self.nominal_offset[0])
            while result > timestamp: 
                m -= self.length_months
                if m<1:
                    m += 12
                    y -= 1
                result = timestamp.replace(year=y, month=m, day=1, hour=0,
                    minute=0) + timedelta(minutes=self.nominal_offset[0])
        else:
            assert(False)
        return result
    def next(self, timestamp):
        timestamp = self.up(timestamp)
        m = timestamp.month
        y = timestamp.year
        m += self.length_months
        while m>12:
            m -= 12
            y += 1
        timestamp = timestamp.replace(year=y, month=m)
        return timestamp + timedelta(minutes=self.length_minutes)
    def previous(self, timestamp):
        timestamp = self.down(timestamp)
        m = timestamp.month
        y = timestamp.year
        m -= self.length_months
        while m<1:
            m += 12
            y -= 1
        timestamp = timestamp.replace(year=y, month=m)
        return timestamp - timedelta(minutes=self.length_minutes)
    def actual_timestamp(self, timestamp):
        m = timestamp.month + self.actual_offset[1]
        y = timestamp.year
        while m>12:
            m -= 12
            y += 1
        while m<1:
            m += 12
            y -= 1
        return timestamp.replace(year=y, month=m) + \
            timedelta(minutes=self.actual_offset[0])
    def containing_interval(self, timestamp):
        result = self.down(timestamp)
        while self.actual_timestamp(result) >= timestamp:
            result = self.previous(result)
        while self.actual_timestamp(result) < timestamp:
            result = self.next(result)
        return result
    def interval_endpoints(self, nominal_timestamp):
        end_date = self.actual_timestamp(nominal_timestamp)
        start_date = self.actual_timestamp(self.previous(nominal_timestamp))
        return start_date, end_date

class _Tsvalue(float):
    def __new__(cls, value, flags=[]):
        return super(_Tsvalue, cls).__new__(cls, value)
    def __init__(self, value, flags=[]):
        self.flags = set(flags)

def strip_trailing_zeros(s):
    last_nonzero = -1
    for i in range(len(s)-1, -1, -1):
        c = s[i]
        if c=='.':
            if last_nonzero==-1: return s[:i]
            else: return s[:last_nonzero+1]
        if c!='0' and last_nonzero==-1: last_nonzero = i
    return s

class Timeseries(dict):

    # Some constants for how timeseries records are distributed in
    # top, middle, and bottom.  Records are appended to bottom, except
    # if bottom would then have more than MAX_BOTTOM plus/minus
    # MAX_BOTTOM_NOISE records (i.e. random noise, evenly distributed
    # between -MAX_BOTTOM_NOISE and +MAX_BOTTOM_NOISE is added to
    # MAX_BOTTOM; this is to avoid reaching circumstances where 20
    # timeseries will be repacked altogether).  If a timeseries is
    # stored entirely from scratch, then all records go to bottom if
    # they are less than MAX_ALL_BOTTOM; otherwise ROWS_IN_TOP_BOTTOM
    # go to top, another as much go to bottom, the rest goes to
    # middle.
    MAX_BOTTOM=100
    MAX_BOTTOM_NOISE=10
    MAX_ALL_BOTTOM=40
    ROWS_IN_TOP_BOTTOM=5

    DT_BASE = datetime(1970,1,1,0,0)

    def __init__(self, id=0, time_step=None, unit=u'', title=u'', timezone=u'',
        variable=u'', precision=None, comment=u''):
        self.id = id
        if time_step:
            self.time_step = time_step
        else:
            self.time_step = TimeStep()
        self.unit = unit
        self.title = title
        self.timezone = timezone
        self.variable = variable
        self.precision = precision
        self.comment = comment
        self.ts_handle = c_void_p(dickinson.ts_create())
        if self.ts_handle==0:
            raise Exception.Create('Could not allocate memory '+
                                   'for time series object.')
#Keep library handle to succesfully free time series when needed
        self.saved_dickinson = dickinson
    def _key_to_timegm(self, key):
        if not isinstance(key, datetime):
            key = datetime_from_iso(key)
        d = key - self.DT_BASE
        return c_longlong(d.days*86400L+d.seconds)
    def _timegm_to_date(self, timegm):
        return self.DT_BASE+\
               timedelta(timegm/86400L,timegm%86400L)
    def __del__(self):
        if self.ts_handle!=0:
            self.saved_dickinson.ts_free(self.ts_handle)
        self.ts_handle=0
    def __len__(self):
        return dickinson.ts_length(self.ts_handle)
    def __delitem__(self, key):
        index_c = dickinson.ts_index_of(self.ts_handle,\
             self._key_to_timegm(key))
        if index_c<0:
            raise KeyError('No such record: '+\
                (isoformat_nosecs(key,' ') if isinstance(key,
                     datetime) else key))
        dickinson.ts_delete_item(self.ts_handle, index_c)
    def __contains__(self, key):
        index_c = dickinson.ts_index_of(self.ts_handle,\
             self._key_to_timegm(key))
        if index_c<0:
            return False
        else:
            return True
    def __setitem__(self, key, value):
        timestamp_c = self._key_to_timegm(key)
        index_c = dickinson.ts_index_of(self.ts_handle, timestamp_c)
        oldflahgs=''
        if index_c>=0:
            arec = dickinson.ts_get_item(self.ts_handle, index_c)
            oldflags = arec.flags
        if isinstance(value, _Tsvalue):
            tsvalue = value
        elif isinstance(value, tuple):
            tsvalue = _Tsvalue(value[0], value[1])
        elif index_c>=0:
            tsvalue = _Tsvalue(value, self[key].flags)
        else:
            tsvalue = _Tsvalue(value, [])
        if fpconst.isNaN(tsvalue):
            null_c=1
            value_c = c_double(0)
        else:
            null_c=0
            value_c = c_double(tsvalue)
        flags_c = c_char_p(' '.join(tsvalue.flags))
        err_no_c = c_int()
        err_str_c = c_char_p()
        if index_c<0:
            index_c = c_int()
            err_no_c = dickinson.ts_insert_record(self.ts_handle, timestamp_c,
                null_c, value_c, flags_c, byref(index_c), byref(err_str_c))
        else:
            err_no_c = dickinson.ts_set_item(self.ts_handle, index_c, null_c,\
                      value_c, flags_c, byref(err_str_c))
        if err_no_c!=0:
            raise Exception('Something wrong occured in dickinson '
                            'function when setting a time series value. '+
                            'Error message: '+repr(err_str_c.value))
    def __getitem__(self, key):
        timestamp_c = self._key_to_timegm(key)
        index_c = dickinson.ts_index_of(self.ts_handle, timestamp_c)
        if index_c<0:
            raise KeyError('No such record: '+\
                (isoformat_nosecs(key,' ') if isinstance(key,
                     datetime) else key))
        arec = dickinson.ts_get_item(self.ts_handle, index_c)
        if arec.null==1:
            value = fpconst.NaN
        else:
            value = arec.value
        flags = arec.flags
        flags = flags.split()
        return _Tsvalue(value, flags)
    def get(self, key, default=None):
        if self.__contains__(key):
            return self.__getitem__(key)
        else:
            return default
    def keys(self):
        a = []
        i = 0
        while i<dickinson.ts_length(self.ts_handle):
            rec = dickinson.ts_get_item(self.ts_handle, c_int(i))
            a.append(self._timegm_to_date(rec.timestamp))
            i+=1
        return a
    def iterkeys(self):
        i = 0
        while i<dickinson.ts_length(self.ts_handle):
            rec = dickinson.ts_get_item(self.ts_handle, c_int(i))
            yield self._timegm_to_date(rec.timestamp)
            i+=1
    __iter__ = iterkeys
    def clear(self):
        i = dickinson.ts_length(self.ts_handle)
        while i>=0:
            dickinson.ts_delete_item(self.ts_handle, i)
            i-=1
    def read(self, fp, line_number=1):
        err_str_c = c_char_p()
        try:
            for line in fp.readlines():
                if dickinson.ts_readline(c_char_p(line), self.ts_handle,
                        byref(err_str_c)):
                    raise ValueError('Error when reading time series '+
                                     'line from I/O: '+repr(err_str_c.value))
                line_number += 1
        except Exception, e:
            e.args = e.args + (line_number,)
            raise
    def write(self, fp, start=None, end=None):
        aline = c_char_p()
        errstr = c_char_p()
        i = 0
        while i<dickinson.ts_length(self.ts_handle):
            rec = dickinson.ts_get_item(self.ts_handle, c_int(i))
            adate = self._timegm_to_date(rec.timestamp)
            if start and adate<start: 
                i+=1
                continue
            if end and adate>end: break 
            if dickinson.ts_writeline(byref(aline), self.ts_handle, c_int(i),
                            c_int(self.precision if self.precision is not None
                            else -9999), byref(errstr))!=0:
                raise IOError('Error when writing time series file, at'
                              'item nr. %d. Error message: %s'%(i,
                                                      repr(errstr.value)))
            fp.write(aline.value)
            i+=1
    def delete_from_db(self, db):
        c = db.cursor()
        c.execute("""DELETE FROM ts_records
                     WHERE id=%d""" % (self.id))
        self.clear()
        c.close()
    def __read_meta_line(self, fp):
        """Read one line from a file format header and return a (name, value)
        tuple, where name is lowercased. Returns ('', '') if the next line is
        blank. Raises ParsingError if next line in fp is not a valid header
        line."""
        line = fp.readline()
        (name, value) = '', ''
        if line.isspace(): return (name, value)
        if line.find('=') > 0:
            (name, value) = line.split('=', 1)
            name = name.rstrip().lower()
            value = value.strip()
        for c in name:
            if c.isspace():
                name = ''
                break
        if not name:
            raise ParsingError(("Invalid file header line"))
        return (name, value)
    def __read_meta(self, fp):
        """Read the headers of a file in file format into the instance
        attributes and return the line number of the first data line of the
        file.
        """
        def read_minutes_months(s):
            """Return a (minutes, months) tuple after parsing a "M,N" string."""
            try:
                (minutes, months) = [int(x.strip()) for x in s.split(',')]
                return minutes, months
            except Exception, e:
                raise ParsingError(('Value should be "minutes, months"'))
#Ignore the BOM_UTF8 byte mark if present by advancing
        if fp.read(len(BOM_UTF8))!=BOM_UTF8:
            fp.seek(-len(BOM_UTF8), SEEK_CUR)
        line_number = 1
        try:
            (name, value) = self.__read_meta_line(fp)
            if name != 'version' or value != '2':
                raise ParsingError(("The first line must be Version=2"))
            line_number += 1
            (name, value) = self.__read_meta_line(fp)
            while name:
                if name == 'unit': self.unit = value
                elif name == 'title': self.title = value
                elif name == 'timezone': self.timezone = value
                elif name == 'variable': self.variable = value
                elif name == 'time_step':
                    minutes, months = read_minutes_months(value)
                    self.time_step.length_minutes = minutes
                    self.time_step.length_months = months
                elif name == 'nominal_offset':
                    self.time_step.nominal_offset = read_minutes_months(value)
                elif name == 'actual_offset':
                    self.time_step.actual_offset = read_minutes_months(value)
                elif name == 'interval_type':
                    it = IntervalType
                    v = value.lower()
                    if v=='sum': self.time_step.interval_type = it.SUM
                    elif v=='average': self.time_step.interval_type = it.AVERAGE
                    elif v=='maximum': self.time_step.interval_type = it.MAXIMUM
                    elif v=='minimum': self.time_step.interval_type = it.MINIMUM
                    elif v=='vector_average': self.time_step.interval_type = it.VECTOR_AVERAGE
                    elif v=='': self.time_step.interval_type = None
                    else: raise ParsingError(("Invalid interval type"))
                elif name == 'precision':
                    try: self.precision = int(value)
                    except TypeError, e: raise ParsingError(e.args)
                elif name == 'comment':
                    if self.comment: self.comment += '\n'
                    self.comment += value.decode('utf-8')
                elif name == 'count': pass
                line_number += 1
                (name, value) = self.__read_meta_line(fp)
                if not name and not value: return line_number
        except ParsingError, e:
            e.args = e.args + (line_number,)
            raise
    def read_file(self, fp):
        line_number = self.__read_meta(fp)
        self.read(fp, line_number=line_number)
    def write_file(self, fp):
        fp.write(u"Version=2\r\n")
        if self.unit:
            fp.write(u"Unit=%s\r\n" % (self.unit,))
        fp.write(u"Count=%d\r\n" % (len(self),))
        if self.title: 
            fp.write(u"Title=%s\r\n" % (self.title,))
        for line in self.comment.splitlines():
            fp.write(u"Comment=%s\r\n" % (line,))
        if self.timezone: 
            fp.write(u"Timezone=%s\r\n" % (self.timezone,))
        if self.time_step.length_minutes or self.time_step.length_months:
            fp.write(u"Time_step=%d,%d\r\n" % (self.time_step.length_minutes,
                                              self.time_step.length_months))
            if self.time_step.nominal_offset:
                fp.write(u"Nominal_offset=%d,%d\r\n" %
                                                self.time_step.nominal_offset)

            fp.write(u"Actual_offset=%d,%d\r\n" %
                                            self.time_step.actual_offset)
        if self.time_step.interval_type:
            fp.write(u"Interval_type=%s\r\n" % ({
                IntervalType.SUM: u"sum", IntervalType.AVERAGE: u"average",
                IntervalType.MAXIMUM: u"maximum", IntervalType.MINIMUM: u"minimum",
                IntervalType.VECTOR_AVERAGE: u"vector_average"
                }[self.time_step.interval_type],))
        if self.variable:
          fp.write(u"Variable=%s\r\n" % (self.variable,))
        if self.precision is not None:
            fp.write(u"Precision=%d\r\n" % (self.precision,))

        fp.write("\r\n")
        self.write(fp)
    def read_from_db(self, db):
        c = db.cursor()
        c.execute("""SELECT top, middle, bottom FROM ts_records
                     WHERE id=%d""" % (self.id))
        r = c.fetchone()
        self.clear()
        if r:
            (top, middle, bottom) = r
            if top:
                self.read(StringIO(top))
                self.read(StringIO(zlib.decompress(middle)))
            self.read(StringIO(bottom))
        c.close()
    def write_to_db(self, db, transaction=None, commit=True):
        if transaction is None: transaction = db
        fp = StringIO()
        if len(self)<Timeseries.MAX_ALL_BOTTOM:
            top = ''
            middle = None
            self.write(fp)
            bottom = fp.getvalue()
        else:
            dates = sorted(self.keys())
            self.write(fp, end = dates[Timeseries.ROWS_IN_TOP_BOTTOM-1])
            top = fp.getvalue()
            fp.truncate(0)
            self.write(fp, start = dates[Timeseries.ROWS_IN_TOP_BOTTOM],
                       end = dates[-(Timeseries.ROWS_IN_TOP_BOTTOM+1)])
            middle = psycopg2.Binary(zlib.compress(fp.getvalue()))
            fp.truncate(0)
            self.write(fp, start = dates[-Timeseries.ROWS_IN_TOP_BOTTOM])
            bottom = fp.getvalue()
        fp.close()
        c = db.cursor()
        c.execute("DELETE FROM ts_records WHERE id=%d" % (self.id))
        c.execute("""INSERT INTO ts_records (id, top, middle, bottom)
                     VALUES (%s, %s, %s, %s)"""  , (self.id, top, middle,
                                                       bottom))
        c.close()
        if commit: transaction.commit()
    def append_to_db(self, db, transaction=None, commit=True):
        """Append the contained records to the timeseries stored in the database."""
        if transaction is None: transaction = db
        if not len(self): return
        c = db.cursor()
        bottom_ts = Timeseries()
        c.execute("SELECT bottom FROM ts_records WHERE id=%d" %
                  (self.id))
        r = c.fetchone()
        if r:
            bottom_ts.read(StringIO(r[0]))
            if max(bottom_ts.keys())>=min(self.keys()):
                raise ValueError(("Cannot append time series: "
                    +"its first record (%s) has a date earlier than the last "
                    +"record (%s) of the timeseries to append to.")
                    % (str(min(self.keys())), str(max(bottom_ts.keys()))))
        max_bottom = Timeseries.MAX_BOTTOM + random.randrange(
            -Timeseries.MAX_BOTTOM_NOISE, Timeseries.MAX_BOTTOM_NOISE)
        if len(bottom_ts) and len(bottom_ts)+len(self) < max_bottom:
            fp = StringIO()
            bottom_ts.write(fp)
            self.write(fp)
            c.execute("""UPDATE ts_records SET bottom=%s
                         WHERE id=%s""", (fp.getvalue(), self.id))
            fp.close()
            if commit: transaction.commit()
        else:
            ts = Timeseries(self.id)
            ts.read_from_db(db)
            ts.append(self)
            ts.write_to_db(db, transaction=transaction, commit=commit)
        c.close()
    def append(self, b):
        if len(self) and len(b) and max(self.keys())>=min(b.keys()):
            raise ValueError(("Cannot append: the first record (%s) of the "
                +"time series to append has a date earlier than the last "
                +"record (%s) of the timeseries to append to.")
                % (str(min(b.keys())), str(max(self.keys()))))
        err_str_c = c_char_p()
        if dickinson.ts_merge(self.ts_handle, b.ts_handle, byref(err_str_c))!=0:
            raise Exception('An exception has occured when trying to '+
                            'merge time series. Error message: '+
                            repr(err_str_c.value))
    def bounding_dates(self):
        if len(self):
            rec1 = dickinson.ts_get_item(self.ts_handle, c_int(0))
            rec2 = dickinson.ts_get_item(self.ts_handle, c_int(len(self)-1))
            return self._timegm_to_date(rec1.timestamp),\
                   self._timegm_to_date(rec2.timestamp)
        else:
            return None
    def items(self):
        a = []
        i = 0
        while i<dickinson.ts_length(self.ts_handle):
            rec = dickinson.ts_get_item(self.ts_handle, c_int(i))
            a.append((self._timegm_to_date(rec.timestamp),
                     _Tsvalue(fpconst.NaN if rec.null else rec.value,
                              rec.flags.split())))
            i+=1
        return a
    def index(self, date, downwards=False):
        timestamp_c = self._key_to_timegm(date)
        if not downwards:
            pos = dickinson.ts_get_next(self.ts_handle, timestamp_c)
        else:
            pos = dickinson.ts_get_prev(self.ts_handle, timestamp_c)
        if pos<0:
            if downwards:
                raise IndexError("There is no item in the timeseries on or before "
                                            +str(date))
            else:
                raise IndexError("There is no item in the timeseries on or after "
                                            +str(date))
        return pos
    def item(self, date, downwards=False):
        rec = dickinson.ts_get_item(self.ts_handle, c_int(self.index(date,
                                                                downwards)))
        return (self._timegm_to_date(rec.timestamp), 
            _Tsvalue(fpconst.NaN if rec.null else rec.value,
                     rec.flags.split()))
    def _get_bounding_indexes(self, start_date, end_date):
        """Return a tuple, (start_index, end_index).  If arguments are None,
        the respective bounding date is considered. The results are the start
        and end indexes in items() of all items that are in the specified
        interval.
        """
        (s, e) = self.bounding_dates()
        if not start_date: start_date =  s
        if not end_date: end_date =  e
        return (self.index(start_date), self.index(end_date, downwards=True))
    def min(self, start_date=None, end_date=None):
        start, end = self._get_bounding_indexes(start_date, end_date)
        items = self.items()
        result = fpconst.NaN
        for i in range(start, end+1):
            if fpconst.isNaN(result) or items[i][1]<result:
                result = items[i][1]
        return result
    def max(self, start_date=None, end_date=None):
        start, end = self._get_bounding_indexes(start_date, end_date)
        items = self.items()
        result = fpconst.NaN
        for i in range(start, end+1):
            if fpconst.isNaN(result) or items[i][1]>result:
                result = items[i][1]
        return result
    def average(self, start_date=None, end_date=None):
        start, end = self._get_bounding_indexes(start_date, end_date)
        items = self.items()
        sum = 0
        divider = 0
        for i in range(start, end+1):
            value = items[i][1]
            if fpconst.isNaN(value): continue
            sum += value
            divider += 1
        if divider:
            return sum/divider
        else:
            return fpconst.NaN
    def aggregate(self, target_step, missing_allowed=0.0, missing_flag=""):
        def aggregate_one_step(d):
            """Return tuple of ((result value, flags), missing) for a single target stamp d."""

            def timedeltadivide(a, b):
                """Divide timedelta a by timedelta b."""
                a = a.days*86400+a.seconds
                b = b.days*86400+b.seconds
                return a/b

            d_start_date, d_end_date = target_step.interval_endpoints(d)
            start_nominal = self.time_step.containing_interval(d_start_date)
            end_nominal = self.time_step.containing_interval(d_end_date)
            s = start_nominal
            it = target_step.interval_type
            if it in (IntervalType.SUM, IntervalType.AVERAGE):
                aggregate_value = 0.0
            elif it == IntervalType.MAXIMUM: aggregate_value = -1e38
            elif it == IntervalType.MINIMUM: aggregate_value = 1e38
            elif it == IntervalType.VECTOR_AVERAGE: aggregate_value = (0, 0)
            else: assert(False)
            missing = 0.0
            total_components = 0.0
            divider = 0.0
            source_has_missing = False
            while s <= end_nominal:
                s_start_date, s_end_date = self.time_step.interval_endpoints(s)
                used_interval = s_end_date - s_start_date
                unused_interval = timedelta()
                if s_start_date < d_start_date:
                    out = d_start_date - s_start_date
                    unused_interval += out
                    used_interval -= out
                if s_end_date > d_end_date:
                    out = s_end_date - d_end_date
                    unused_interval += out
                    used_interval -= out
                pct_used = timedeltadivide(used_interval,
                                (unused_interval+used_interval))
                total_components += pct_used
                if fpconst.isNaN(self.get(s, fpconst.NaN)):
                    missing += pct_used
                    s = self.time_step.next(s)
                    continue
                divider += pct_used
                if missing_flag in self[s].flags:
                    source_has_missing = True
                if it in (IntervalType.SUM, IntervalType.AVERAGE):
                    aggregate_value += self.get(s,0)*pct_used
                elif it == IntervalType.MAXIMUM:
                    if pct_used > 0: aggregate_value = max(aggregate_value, self[s])
                elif it == IntervalType.MINIMUM:
                    if pct_used > 0: aggregate_value = min(aggregate_value, self[s])
                elif it == IntervalType.VECTOR_AVERAGE:
                    aggregate_value = (aggregate_value[0]+cos(self[s]/180*pi)*pct_used, 
                                       aggregate_value[1]+sin(self[s]/180*pi)*pct_used)
                else:
                    assert(False)
                s = self.time_step.next(s)
            flag = []
            if missing/total_components > missing_allowed/total_components+1e-5 or abs(
                                       missing-total_components) < 1e-36:
                aggregate_value = fpconst.NaN
            else:
                if (missing/total_components > 1e-36) or\
                  source_has_missing: flag = [missing_flag]
                if it == IntervalType.AVERAGE: aggregate_value /= divider
                elif it == IntervalType.VECTOR_AVERAGE:
                    aggregate_value = atan2(aggregate_value[1], aggregate_value[0])/pi*180
                    while aggregate_value<0: aggregate_value+=360
                    if abs(aggregate_value-360)<1e-7: aggregate_value=0
            return (aggregate_value, flag), missing

        source_start_date, source_end_date = self.bounding_dates()
        target_start_date = target_step.previous(source_start_date)
        target_end_date = target_step.next(source_end_date)
        result = Timeseries(time_step=target_step)
        missing = Timeseries(time_step=target_step)
        d = target_start_date
        while d <= target_end_date:
            result[d], missing[d] = aggregate_one_step(d)
            d = target_step.next(d)
        while fpconst.isNaN(result.get(target_start_date, 0)):
            del result[target_start_date]
            del missing[target_start_date]
            target_start_date = target_step.next(target_start_date)
        while fpconst.isNaN(result.get(target_end_date, 0)):
            del result[target_end_date]
            del missing[target_end_date]
            target_end_date = target_step.previous(target_end_date)
        return result, missing
