# -*- coding: utf-8 -*-
# from datetime import datetime
import logging

from facebook import GraphAPI, GraphAPIError
from flask.ext.sqlalchemy import SQLAlchemy
from itertools import repeat
import requests
import re
from datetime import datetime, timedelta
import time
import pickle
from googlemaps import geocoding, Client as GMapClient

from ..models import City
from ..utilities import init_flask

FB_PAGE_NEWS_UPDATES_CODE = '601595769890923'
FB_PAGE_TARGET = '135305907147204'

# APP_... from app dashboard
FB_APP_ID = '156391101644523'
FB_APP_SECRET = '8012d05ce67928871140ca924f29b58f'

# https://developers.facebook.com/tools/explorer/156391101644523?method=POST&path=135305907147204%2Ffeed&version=v2.11
FB_PAGE_ACCESS_TOKEN = 'EAACOPKQPPusBAJZCYUgZCaRXUWkkElpLq3SCPWLNwZBqHNSHfLWqryLlwJMiJupJb8Dm67DGYaFbH5JmjEJMnLgVQTyphO79CtV8wudYigdRmnXHh44t0XzVO6qCtdB9DsiYYpQs90cacZAHiw3QVoE1gUApJ72UfjuIC7yaWZBZB5Wr3ZBN0OYjgLD4fEtFx0ZD'

MADA_END_ADDRESS_MARKER = u'חובשים ופראמדיקים'
MADA_TEXT_INDICATOR = u'התקבל דיווח במוקד 101 של מד"א במרחב'
EHUD_TEXT_INDICATOR = u'דוברות איחוד הצלה:'
IGNORE_STORY_INDICATOR = u'‎עדכוני חדשות‎ shared a link'
GOOGLE_API_KEY = 'AIzaSyBzx2_S-94XmcLqWpydr9EZRmouik0x__s'
STR_LEN_AT_ROAD = len(u'בכביש')
STR_LEN_TO_DIRECTION = len(u'לכיוון')
KEY_EVENT_ADDRESS = 'addr'
KEY_EVENT_DESCRIBE = 'desc'
MAX_DIFERENCE_IN_DAYS = 1

app = init_flask()
db = SQLAlchemy(app)

_city_criteria = db.session.query(City.search_heb, City.shortname_heb, City.symbol_code) \
    .order_by(City.search_priority.desc()) \
    .all()

# ##################################################################
class ProcessHandler(object):
    def __init__(self, interval):

        self.tm_min = time.time() - 60 * 60 * interval
        try:
            self._posts = []
            self._parsers_dict = {
                MADA_TEXT_INDICATOR: MadaParser(),
                EHUD_TEXT_INDICATOR: EhudHazalaParser()
            }
            self._api = GraphAPI()
            self._api.access_token = self._api.get_app_access_token(FB_APP_ID, FB_APP_SECRET)
            self._gmapclient = GMapClient(GOOGLE_API_KEY)
        except GraphAPIError as e:
            logging.error('can not obtain access token,abort (%s)'.format(e.message))
            raise e

    def has_access(self):
        return self._api.access_token is not None

    @staticmethod
    def text_timestamp_to_datetime(timestamp):
        if re.search('\+0000$',timestamp) is not None:
            timestamp = timestamp.split('+')[0]
        return datetime.strptime(timestamp, u'%Y-%m-%dT%H:%M:%S')

    @staticmethod
    def is_inrange(timestamp):
        diff_time = datetime.utcnow() - ProcessHandler.text_timestamp_to_datetime(timestamp)
        if diff_time.days > MAX_DIFERENCE_IN_DAYS:
            return False
        return True

    def read_data(self):
        with open('dumppost139.txt', 'rb') as fh:
            self._posts = pickle.Unpickler(fh).load()
        return True

        if not self.has_access():
            return False
        try:
            response_posts = self._api.get_object(FB_PAGE_NEWS_UPDATES_CODE + '/posts')
            self._posts = response_posts['data']
            for idx in repeat(None, 10):
                response_posts = requests.request('GET', response_posts['paging']['next']).json()
                self._posts += response_posts['data']
        except GraphAPIError as e:
            logging.error('can not obtain posts,abort (%s)'.format(e.message))
            return False
        return True

    def write_port(self, message, link=None):
        try:
            args = {'message': message, 'access_token' : FB_PAGE_ACCESS_TOKEN,'scope':'publish_actions'}
            if link is not None:
                args['link'] = link
            response_posts = self._api.put_object(FB_PAGE_TARGET ,'feed', **args)
        except GraphAPIError as e:
            if e.code == 506:
                logging.warn('duplicate message, ignore (%s)'.format(e.message))
                return True
            logging.error('can not post message to feed, abort (%s)'.format(e.message))
            return False
        return True

    def place_post(self, descriptor):
        self.write_port(descriptor.__str__())
        # print descriptor.__str__()
        pass

    def get_provider_parser(self, msg):
        if msg.find(MADA_TEXT_INDICATOR) >= 0:
            return self._parsers_dict[MADA_TEXT_INDICATOR]
        if msg.find(EHUD_TEXT_INDICATOR) >= 0:
            return self._parsers_dict[EHUD_TEXT_INDICATOR]
        return None

    def process(self):
        for post in self._posts:
            if self.is_inrange(post['created_time']) is False:
                continue
            if 'message' not in post:
                if 'story' in post:
                    post['message'] = post['story']
                else:
                    continue

            parser = self.get_provider_parser(post['message'])
            if parser is None:
                continue
            descriptor = parser.extract(post)
            if descriptor is not None and descriptor.has_address():
                descriptor.post_id = post['id']
                descriptor.post_tm = post['created_time']
                for addresse in descriptor.addresses:
                    geocode = geocoding.geocode(self._gmapclient, address=addresse, region='il')
                    if len(geocode) > 0:
                        location = geocode[0]['geometry']['location']
                        descriptor.sel_addr = addresse
                        descriptor.lat = location['lat']
                        descriptor.lng = location['lng']
                        self.place_post(descriptor)
                        break


class EventDescriptor(object):
    def __init__(self, msg, subject):
        self.msg = msg
        self.subject = subject
        self.addresses = []
        self.sel_addr = u'NA'
        self.lat = 0
        self.lng = 0
        self.post_id = 0
        self.post_tm = 0

    def __str__(self):
        return u'desc:{5}\naddress:{0}\nlat:{1},long:{2}\nID:{3},Time:{4}'.format(self.sel_addr, self.lat, self.lng, self.post_id,
                                                                        self.post_tm,self.desc)

    def set_describe(self, text):
        self.desc = text

    def add_address(self, text):
        self.addresses.append(text)

    def has_address(self):
        return len(self.addresses) > 0


class ProviderParserBase(object):
    @staticmethod
    def _find_one_of(msg, cases):
        if not isinstance(cases, basestring):
            for case in cases:
                spot = msg.find(case)
                if spot > 0:
                    return {'at': spot, 'of': case, 'end': spot + len(case)}
        else:
            spot = msg.find(cases)
            if spot > 0:
                return {'at': spot, 'of': cases, 'end': spot + len(cases)}
        return None

    @staticmethod
    def _remove_number_of_words(msg, words=1, front=True):
        parts = msg.strip(u' ').split()
        while (words > 0):
            words -= 1
            if front:
                del (parts[-1])
            else:
                del (parts[0])
        return u' '.join(parts)


class EhudHazalaParser(ProviderParserBase):
    @staticmethod
    def _dot_split_first_part(msg):
        parts = msg.split(u'.')
        return parts[0]

    @staticmethod
    def _prepare_address_cases(descriptor, address, append=''):
        address = append + address.replace(u'בסמוך ל', u'ליד ').strip(u'.')
        descriptor.add_address(address)
        if address.find(u'צומת') > 0:
            descriptor.add_address(address.replace(u'צומת', ''))

    def extract(self, post):
        return None
        # msg = post['message']
        #
        # relative_case_of = self._find_one_of(msg, (u'נפגע מ',u'נפגעה מ',u'תאונת דרכים', u'על תאונה', u'רוכב אופנוע',u'רוכב קטנוע',u'פגיעת רכב',u'תאונה עם'))
        # if relative_case_of is None:
        #     return None
        #
        # descriptor = EventDescriptor(msg, '')
        # print msg
        # # parts = self._find_one_of(msg,(u'טופל ע"י',u'טיפול רפואי ראשוני',u'טיפול ראשוני'))
        # print relative_case_of['of']
        # roud_of = self._find_one_of(msg, (u' בכביש',u' כביש'))
        # if roud_of is not None:
        #     parts =msg.split(relative_case_of['of'])
        #     descriptor.set_describe(parts[0])
        #     # re.search(r'[{0}]\D+\d+' % u',.' +u' כביש',.............
        #     roud_parts = parts[-1].split(roud_of['of'])
        #     address = self._dot_split_first_part(roud_parts[1])
        #     self._prepare_address_cases(descriptor,address,u'כביש ')
        #
        # return descriptor


class MadaParser(ProviderParserBase):
    @staticmethod
    def _extract_describe(text, from_pos):
        parts = text[:from_pos].strip(u' ')
        parts = parts.split()
        del (parts[0])
        del (parts[0])
        return u' '.join(parts)

    def extract(self, post):

        msg = post['message']
        relative_case_of = self._find_one_of(msg, (u'נפגע מרכב', u'על תאונה', u'רוכב אופנוע'))
        if relative_case_of is None:
            return None

        subject = msg[
                  msg.find(MADA_TEXT_INDICATOR) + len(MADA_TEXT_INDICATOR):msg.find(MADA_END_ADDRESS_MARKER)]
        descriptor = EventDescriptor(msg, subject)

        # near_of = self.find_address(subject,(u'בסמוך',u'סמוך'))
        roud_of = self._find_one_of(subject, (u'בכביש'))
        if roud_of is not None:
            descriptor.set_describe(self._extract_describe(subject, roud_of['at']))
            searchin = subject[roud_of['end']:].strip(u' .')
            searchin_parts = searchin.split()
            searchin_parts.insert(0, u'כביש')
            try:
                part_pos = searchin_parts.index(u'לכיוון')
                searchin_parts = searchin_parts[:part_pos]
                result = u' '.join(searchin_parts)
            except ValueError as e:
                return descriptor
            descriptor.add_address(result.replace(u'על גשר', '').replace(u'סמוך ל', ''))
            return descriptor
        address_of = self._find_one_of(subject, (u' ברחוב', u" ברח'", u' בשדרות', u" בשד'", u' בדרך'))
        if address_of is not None:
            descriptor.set_describe(self._extract_describe(subject, address_of['at']))
            searchin = subject[address_of['end']:].strip(u' .')
            for city in _city_criteria:
                criteria = u'ב' + city.search_heb
                criteria_pos = searchin.find(criteria)
                if city.shortname_heb != None and criteria_pos < 0:  # if we have short name and we can not find match
                    criteria = u'ב' + city.shortname_heb
                    criteria_pos = searchin.find(criteria)
                if criteria_pos >= 0:
                    descriptor.add_address(searchin[:criteria_pos].strip() + u' ' + city.search_heb)
                    break

        return descriptor


def main(interval):
    handler = ProcessHandler(interval)
    if not handler.read_data():
        logging.debug('no data to process,abort')
        return
    handler.process()
