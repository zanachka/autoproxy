# -*- coding: utf-8 -*-
import scrapy
import sys
import logging
from IPython import embed
import re
import json
from proxy_objects import Proxy
from storage_manager import StorageManager

class ProxydbSpider(scrapy.Spider):
    name = 'proxydb'
    allowed_domains = ['proxydb.net']
    start_urls = ['http://proxydb.net/']

    def __init__(self,*args,**kwargs):
        self.count = int(kwargs.get('count',1))
        self.storage_mgr = StorageManager()
    
    def start_requests(self):
        for i in range(self.count):
            request = scrapy.Request(url='http://proxydb.net/', dont_filter=True)
            logging.info("GET %s" % request.url)
            yield request

    def parse(self,response):
        with open('./proxydb.html','w') as ofh:
            ofh.write(response.body.decode('utf-8'))
        
"""
    def parse(self, response):
        tds = response.xpath('//div[@class="table-responsive"]/table[contains(@class,"table-hover")]//tr/td')
        embed()
        for tr in trs:
            address_port = tr.xpath('./td[1]/a/text()').extract_first()
            embed()
            protocol = tr.xpath('./td[5]/text()').extract_first()
            ap_res = re.search(r'^([\d\.]+)\:(\d+)$', address_port)
            address = ap_res.group(1)
            port = int(ap_res.group(2))
            embed()

            self.storage_mgr.new_proxy(Proxy(address=address, port=port, protocol=protocol))
"""