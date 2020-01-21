import redis
import psycopg2
import sys
import os
import logging
import time
import json
import re
from functools import wraps
from copy import deepcopy
from datetime import datetime, timedelta
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
import traceback

from psycopg2.extras import DictCursor
from psycopg2 import sql
from IPython import embed
from scrapy_autoproxy.proxy_objects import Proxy, Detail, Queue
from scrapy_autoproxy.util import parse_domain

from scrapy_autoproxy.config import configuration
app_config = lambda config_val: configuration.app_config[config_val]['value']

SEED_QUEUE_ID = app_config('seed_queue')
AGGREGATE_QUEUE_ID = app_config('aggregate_queue')
LIMIT_ACTIVE = app_config('active_proxies_per_queue')
LIMIT_INACTIVE = app_config('inactive_proxies_per_queue')

SEED_QUEUE_DOMAIN = parse_domain(app_config('designated_endpoint'))
AGGREGATE_QUEUE_DOMAIN = 'RESERVED_AGGREGATE_QUEUE'
ACTIVE_LIMIT = app_config('active_proxies_per_queue')
INACTIVE_LIMIT = app_config('inactive_proxies_per_queue')
TEMP_ID_COUNTER = 'temp_id_counter'

BLACKLIST_THRESHOLD = app_config('blacklist_threshold')
MAX_BLACKLIST_COUNT = app_config('max_blacklist_count')
BLACKLIST_TIME = app_config('blacklist_time')
MAX_DB_CONNECT_ATTEMPTS = app_config('max_db_connect_attempts')
DB_CONNECT_ATTEMPT_INTERVAL = app_config("db_connect_attempt_interval")
PROXY_INTERVAL = app_config('proxy_interval')
LAST_USED_CUTOFF = datetime.utcnow() - timedelta(seconds=PROXY_INTERVAL)
NEW_DETAILS_SET_KEY = 'new_details'
INITIAL_SEED_COUNT = app_config('initial_seed_count')



# decorator for RedisManager methods
def block_if_syncing(func):
    @wraps(func)
    def wrapper(self,*args,**kwargs):
        while self.is_syncing() and not self.is_sync_client():
            logging.info('awaiting sync...')
            time.sleep(5)
        return(func(self,*args,**kwargs))
    return wrapper

class PostgresManager(object):
    def __init__(self):
        connect_params = configuration.db_config
        connect_params.update({'cursor_factory':DictCursor})
        self.connect_params = connect_params
        self.connect_attempts = 0

    def new_connection(self):
        if self.connect_attempts < MAX_DB_CONNECT_ATTEMPTS:
            try:
                conn = psycopg2.connect(**self.connect_params)
                conn.set_session(autocommit=True)
            except Exception as e:
                self.connect_attempts +=1
                logging.info("Connection attempt %s/%s" % (self.connect_attempts, MAX_DB_CONNECT_ATTEMPTS))
                logging.info("Might need a little time for the database to initialiaze.  Sleeping for %s seconds" % DB_CONNECT_ATTEMPT_INTERVAL)
                time.sleep(DB_CONNECT_ATTEMPT_INTERVAL)
                return self.new_connection()

            return conn
        else:
            raise Exception("Failed to connect to the database.")
    
    def cursor(self):
        return self.new_connection().cursor()

    def do_query(self, query, params=None):
        cursor = self.cursor()
        cursor.execute(query,params)
        try:
            data = cursor.fetchall()
            return data
        except Exception:
            pass
        cursor.close()

    def update_detail(self,obj,cursor=None):
        table_name = sql.Identifier('details')
        obj_dict = obj.to_dict()
        where_sql = sql.SQL("{0}={1}").format(sql.Identifier('detail_id'),sql.Placeholder('detail_id'))        

        if 'detail_id' not in obj_dict:
            if 'queue_id' not in obj_dict or 'proxy_id' not in obj_dict:
                raise Exception("cannot update detail without a detail id, queue id, or proxy id")
            where_sql = sql.SQL("{0}={1} AND {2}={3}").format(sql.Identifier('queue_id'),sql.Placeholder('queue_id'),sql.Identifier('proxy_id'),sql.Placeholder('proxy_id'))        
            
            

        set_sql = sql.SQL(', ').join([sql.SQL("{0}={1}").format(sql.Identifier(k),sql.Placeholder(k)) for k in obj_dict.keys()])
        update = sql.SQL('UPDATE {0} SET {1} WHERE {2}').format(table_name,set_sql,where_sql)
        if cursor is not None:
            cursor.execute(update,obj.to_dict())
        else:
            self.do_query(update,obj.to_dict())

    def insert_object(self,obj,table,returning, cursor=None):
        table_name = sql.Identifier(table)
        column_sql = sql.SQL(', ').join(map(sql.Identifier, obj.to_dict().keys()))
        placeholder_sql = sql.SQL(', ').join(map(sql.Placeholder,obj.to_dict()))
        returning = sql.Identifier(returning)
        
        insert = sql.SQL('INSERT INTO {0} ({1}) VALUES ({2}) RETURNING {3}').format(table_name,column_sql,placeholder_sql, returning)


        if cursor is not None:
            cursor.execute(insert,obj.to_dict())
        else:
            self.do_query(insert,obj.to_dict())



    def insert_detail(self,detail, cursor=None):
        self.insert_object(detail,'details', 'detail_id',cursor)

    def insert_queue(self,queue, cursor=None):
        self.insert_object(queue,'queues','queue_id', cursor)

    def insert_proxy(self,proxy,cursor=None):
        self.insert_object(proxy,'proxies','proxy_id',cursor)

    def init_seed_details(self):
        seed_count = self.do_query("SELECT COUNT(*) as c FROM details WHERE queue_id=%(queue_id)s", {'queue_id':SEED_QUEUE_ID})[0]['c']
        logging.info("initializing seed proxies")
        cursor = self.cursor()
        if seed_count == 0:
            proxy_ids = [p['proxy_id'] for p in self.do_query("SELECT proxy_id FROM proxies")]
            for proxy_id in proxy_ids:
                insert_detail = "INSERT INTO details (proxy_id,queue_id) VALUES (%(proxy_id)s, %(queue_id)s);"
                params = {'proxy_id': proxy_id, 'queue_id': SEED_QUEUE_ID}
                cursor.execute(insert_detail,params)

        
        query = """
        BEGIN;
        LOCK TABLE details IN EXCLUSIVE MODE;
        SELECT setval('details_detail_id_seq', COALESCE((SELECT MAX(detail_id)+1 FROM details),1), false);
        COMMIT;
        """
        
        cursor.execute(query)
        cursor.close()
        logging.info("done initializing seeds")
        
    def get_seed_details(self):
        self.init_seed_details()

        params = {'seed_queue_id': SEED_QUEUE_ID}
        query= """
            SELECT * FROM details 
            WHERE queue_id=%(queue_id)s
            AND active=%(active)s
            AND last_used < %(last_used_cutoff)s
            ORDER BY last_used ASC
            LIMIT %(limit)s;
            """
        a_params = {"queue_id":SEED_QUEUE_ID, "active":True,"limit": INITIAL_SEED_COUNT}
        ia_params = {"queue_id":SEED_QUEUE_ID, "active":False,"limit": INITIAL_SEED_COUNT}
        a_params['last_used_cutoff'] = LAST_USED_CUTOFF
        ia_params['last_used_cutoff'] = LAST_USED_CUTOFF
        active =  [Detail(**d) for d in self.do_query(query,a_params)]
        inactive = [Detail(**d) for d in self.do_query(query,ia_params)]
        
        return active + inactive

    def get_non_seed_details(self,queue_id):
        if queue_id is None:
            return []
        query= """
            SELECT * FROM details 
            WHERE queue_id = %(queue_id)s
            AND active=%(active)s
            AND last_used < %(last_used_cutoff)s
            ORDER BY last_used ASC
            LIMIT %(limit)s;
            """
        
        active_params = { 
            'queue_id': queue_id,
            'active': True,
            'last_used_cutoff': LAST_USED_CUTOFF,
            'limit': ACTIVE_LIMIT
        }

        inactive_params = {
            'queue_id': queue_id,
            'active': False,
            'last_used_cutoff': LAST_USED_CUTOFF,
            'limit': INACTIVE_LIMIT
        }

        active = [Detail(**d) for d in self.do_query(query, active_params)]
        logging.info("fetched %s details ")
        inactive = [Detail(**d) for d in self.do_query(query, inactive_params)]

        return active + inactive
        

    def init_seed_queues(self):
        logging.info("Initializing queues...")
        if(SEED_QUEUE_ID == AGGREGATE_QUEUE_ID):
            raise Exception("aggregate_queue and seed_queue cannot share the same id.  Check app_config.json")
        
        seed_queue = Queue(domain=SEED_QUEUE_DOMAIN,queue_id=SEED_QUEUE_ID)
        agg_queue = Queue(domain=AGGREGATE_QUEUE_DOMAIN, queue_id=AGGREGATE_QUEUE_ID)

        query = "SELECT queue_id from queues WHERE domain = %(domain)s"
        db_seed = self.do_query(query, {'domain':SEED_QUEUE_DOMAIN})
        db_agg = self.do_query(query, {'domain': AGGREGATE_QUEUE_DOMAIN})
        
        if len(db_seed) == 0:
            self.insert_queue(seed_queue)
        elif db_seed[0]['queue_id'] != SEED_QUEUE_ID:
            raise Exception("seed_queue id mismatch. seed_queue should be set to %s  Check app_config.json" % db_seed[0]['queue_id'])
        
        if len(db_agg) == 0:
            self.insert_queue(agg_queue)
        elif(db_agg[0]['queue_id'] != AGGREGATE_QUEUE_ID):
            raise Exception("aggregate queue_id mismatch.  aggregate_queue should be set to %s  Check app_config.json" % db_agg[0]['queue_id'])

        cursor = self.cursor()
        query = """
        BEGIN;
        LOCK TABLE queues IN EXCLUSIVE MODE;
        SELECT setval('queues_queue_id_seq', COALESCE((SELECT MAX(queue_id)+1 FROM queues),1), false);
        COMMIT;
        """
        cursor.execute(query)
        cursor.close()
        logging.info("Finished initializing queue.")
        


    def get_queues(self):
        self.init_seed_queues()
        return [Queue(**r) for r in self.do_query("SELECT * FROM queues;")]
        

    def get_proxies(self):
        return [Proxy(**p) for p in self.do_query("SELECT * FROM proxies")]

    def get_detail_by_queue_and_proxy(self,queue_id,proxy_id):
        query = "SELECT * FROM details WHERE proxy_id=%(proxy_id)s AND queue_id=%(queue_id)s"
        params = {'queue_id': queue_id, 'proxy_id':proxy_id}
        cursor = self.cursor()
        cursor.execute(query,params)
        detail_data = cursor.fetchone()
        if detail_data is None:
            cursor.close()
            return None
        detail = Detail(**detail_data)
        cursor.close()
        return detail

    def get_proxy_by_address_and_port(self,address,port):
        query = "SELECT * FROM proxies where address=%(address)s AND port=%(port)s"
        params = {'address': address, 'port':port}
        cursor = self.cursor()
        cursor.execute(query,params)
        proxy_data = cursor.fetchone()
        if proxy_data is None:
            cursor.close()
            return None
        proxy = Proxy(**proxy_data)
        cursor.close()
        return proxy

    

        

class Redis(redis.Redis):
    def __init__(self,*args,**kwargs):
        pool = redis.BlockingConnectionPool(decode_responses=True, *args, **kwargs)
        super().__init__(connection_pool=pool)

class RedisDetailQueueEmpty(Exception):
    pass
class RedisDetailQueueInvalid(Exception):
    pass

class RedisDetailQueue(object):
    def __init__(self,queue_key,active=True):
        self.redis_mgr = RedisManager()

        self.redis = self.redis_mgr.redis
        self.queue_key = queue_key
        self.active = active
        active_clause = "active"
        if not active:
            active_clause = "inactive"
        self.redis_key = 'redis_%s_detail_queue_%s' % (active_clause, queue_key)

    def reload(self):
        details = self.redis_mgr.get_all_queue_details(self.queue_key)
        self.clear()
        for detail in details:
            if detail.active == self.active:
                self.enqueue(detail)



    @classmethod
    def new(cls,queue_key,active):
        return cls(queue_key,active)


    def _update_blacklist_status(self,detail):
        if detail.blacklisted:
            last_used = detail.last_used
            now = datetime.utcnow()
            delta_t = now - last_used
            if delta_t.microseconds/1000 > BLACKLIST_TIME and detail.blacklisted_count < MAX_BLACKLIST_COUNT:
                logging.info("unblacklisting detail")
                detail.blacklisted = False

                self.redis.sadd('changed_details',detail.detail_key)
        
    
    def is_empty(self):
        if not self.redis.exists(self.redis_key):
            return True
        elif self.redis.llen(self.redis_key) == 0:
            return True
        else:
            return False

    def enqueue(self,detail):
        self._update_blacklist_status(detail)
        if detail.blacklisted:
            logging.warn("detail is blacklisted, will not enqueue")
            return

        proxy = RedisManager().get_proxy(detail.proxy_key)
        if 'socks' in proxy.protocol:
            logging.warn("not supporting socks proxies right now, will not enqueue")
            return
        
        detail_key = detail.detail_key
        detail_queue_key = self.redis.hget(detail_key,'queue_key')

        if detail_queue_key != self.queue_key:
            raise RedisDetailQueueInvalid("No such queue key for detail")
        
        if detail.active and not self.active:
            correct_queue = self.new(self.queue_key, not self.active)
            correct_queue.enqueue(detail)
            return
        elif not detail.active and self.active:
            correct_queue = self.new(self.queue_key, not self.active)
            correct_queue.enqueue(detail)
            return
        self.redis.rpush(self.redis_key,detail_key)



    def dequeue(self,requeue=True):
        if self.is_empty():
            raise RedisDetailQueueEmpty("No proxies available for queue key %s" % self.queue_key)
        detail = Detail(**self.redis.hgetall(self.redis.lpop(self.redis_key)))

        if requeue:
            self.enqueue(detail)
        return detail

    def length(self):
        return self.redis.llen(self.redis_key)

    def clear(self):
        self.redis.delete(self.redis_key)
    

class RedisManager(object):
    def __init__(self):
        self.redis = Redis(**configuration.redis_config)
        self.dbh = PostgresManager()

        if len(self.redis.keys()) == 0:
            lock = self.redis.lock('syncing')
            if lock.acquire(blocking=True, blocking_timeout=0):
                self.redis.client_setname('syncer')
                self.sync_from_db()
                self.redis.client_setname('')
                lock.release()

    def is_sync_client(self):
        return self.redis.client_getname() == 'syncer'

    def is_syncing(self):
        return self.redis.get('syncing') is not None

    @block_if_syncing
    def sync_from_db(self):
        logging.info("Syncing proxy data from database to redis...")
        self.redis.set("%s_%s" % (TEMP_ID_COUNTER, 'q'),0)
        self.redis.set("%s_%s" % (TEMP_ID_COUNTER, 'p'),0)
        self.redis.set("%s_%s" % (TEMP_ID_COUNTER, 'd'),0)

        queues = self.dbh.get_queues()
        logging.info("loaded %s queues from database" % len(queues))
        logging.info("queues:")
        logging.info(queues)

        for q in queues:
            self.register_queue(q)

        logging.info("fetching proxies from database...")
        proxies = self.dbh.get_proxies()
        logging.info("got %s proxies from database." % len(proxies))
        logging.info("registering proxies...")
        for p in proxies:
            self.register_proxy(p)
        logging.info("registered proxies.")
        
        logging.info("fetching seed details from database...")
        seed_details = self.dbh.get_seed_details()
        logging.info("got %s seed details from database." % len(seed_details))

        logging.info("registering seed details...")
        for d in seed_details:
            self.register_detail(d)
        logging.info("registered seed details.")

        seed_queue = self.get_queue_by_id(SEED_QUEUE_ID)
        seed_rdq_active = RedisDetailQueue(seed_queue.queue_key,active=True)
        seed_rdq_inactive = RedisDetailQueue(seed_queue.queue_key,active=False)

        seed_rdq_active.reload()
        seed_rdq_inactive.reload()
        #logging.info("fetching non-seed details from database...")

        #other_details = []
        """
        for q in queues:
            logging.info("queue id %s" % q.queue_id)
            if q.queue_id != SEED_QUEUE_ID and q.queue_id != AGGREGATE_QUEUE_ID:
                details = self.dbh.get_non_seed_details(queue_id=q.queue_id)
                other_details.extend(details)
        
        
        logging.info("got %s proxy details from database." % len(other_details))
        logging.info("registering proxy details...")
        for d in other_details:
            self.register_detail(d)
        logging.info("registerd proxy details.")
        """
        logging.info("sync complete.")
    
    @block_if_syncing
    def register_object(self,key,obj):
        redis_key = key
        if obj.id() is None:
            temp_counter_key = "%s_%s" % (TEMP_ID_COUNTER, key)
            redis_key += 't_%s' % self.redis.incr(temp_counter_key)
        else:
            redis_key += '_%s' % obj.id()
        
        self.redis.hmset(redis_key,obj.to_dict(redis_format=True))
        return redis_key

    @block_if_syncing
    def register_queue(self,queue):
        queue_key = self.register_object('q',queue)
        self.redis.hmset(queue_key, {'queue_key': queue_key})
        
        return Queue(**self.redis.hgetall(queue_key))

    @block_if_syncing
    def register_proxy(self,proxy):
        proxy_key = self.register_object('p',proxy)
        self.redis.hmset(proxy_key, {'proxy_key': proxy_key})


        return Proxy(**self.redis.hgetall(proxy_key))
    
    @block_if_syncing
    def register_detail(self,detail):
        if detail.proxy_key is None or detail.queue_key is None:
            raise Exception('detail object must have a proxy and queue key')
        if not self.redis.exists(detail.proxy_key) or not self.redis.exists(detail.queue_key):
            raise Exception("Unable to locate queue or proxy for detail")

        detail_key = detail.detail_key
        
        if self.redis.exists(detail.detail_key):
            logging.warn("Detail already exists")
            return Detail(**self.redis.hgetall(detail_key))
        else:
            self.redis.hmset(detail_key, detail.to_dict(redis_format=True))
        
        relational_keys = {'proxy_key': detail.proxy_key, 'queue_key': detail.queue_key}
        self.redis.hmset(detail_key, relational_keys)
        
        return Detail(**self.redis.hgetall(detail_key))

    @block_if_syncing
    def get_detail(self,redis_detail_key):
        return Detail(**self.redis.hgetall(redis_detail_key))

    @block_if_syncing
    def get_all_queues(self):
        return [Queue(**self.redis.hgetall(q)) for q in self.redis.keys('q*')]

    def get_queue_by_domain(self,domain):
        queue_dict = {q.domain: q for q in self.get_all_queues()}
        if domain in queue_dict:
            return queue_dict[domain]
        
        return self.register_queue(Queue(domain=domain))

    def get_queue_by_id(self,qid):
        lookup_key = "%s_%s" % ('q',qid)
        if not self.redis.exists(lookup_key):
            raise Exception("No such queue with id %s" % qid)
        return Queue(**self.redis.hgetall(lookup_key))            

    def get_proxy(self,proxy_key):
        return Proxy(**self.redis.hgetall(proxy_key))

    def update_detail(self,detail):
        self.redis.hmset(detail.detail_key,detail.to_dict(redis_format=True))
        self.redis.sadd('changed_details',detail.detail_key)

    def get_proxy_by_address_and_port(self,address,port):
        proxy_keys = self.redis.keys('p*') + self.redis.keys('pt*')
        all_proxies = [Proxy(**self.redis.hgetall(pkey)) for pkey in proxy_keys]
        proxy_dict = {"%s:%s" % (proxy.address,proxy.port): proxy for proxy in all_proxies}
        search_key = "%s:%s" % (address,port)
        return proxy_dict.get(search_key,None)

    def get_all_queue_details(self, queue_key):

        key_match = 'd_%s*' % queue_key
        keys = self.redis.keys(key_match)

        details = [Detail(**self.redis.hgetall(key)) for key in keys]
        return details
            


class StorageManager(object):
    def __init__(self):
        self.redis_mgr = RedisManager()
        self.db_mgr = PostgresManager()

    def new_proxy(self,proxy):
        existing = self.redis_mgr.get_proxy_by_address_and_port(proxy.address,proxy.port)
        if existing is None:
            logging.info("registering new proxy %s" % proxy.urlify())
            new_proxy = self.redis_mgr.register_proxy(proxy)
            new_detail = Detail(proxy_key=new_proxy.proxy_key, queue_id=SEED_QUEUE_ID)
            self.redis_mgr.register_detail(new_detail)
            self.redis_mgr.redis.sadd(NEW_DETAILS_SET_KEY,new_detail.detail_key)
        else:
            logging.info("proxy already exists in cache/db")

    def get_seed_queue(self):
        return self.redis_mgr.get_queue_by_id(SEED_QUEUE_ID)

    def create_queue(self,url):
        logging.info("creating queue for %s" % url)
        domain = parse_domain(url)
        all_queues_by_domain = {queue.domain: queue for queue in self.redis_mgr.get_all_queues()}
        if domain in all_queues_by_domain:
            logging.warn("Trying to create a queue that already exists.")
            return all_queues_by_domain[domain]
        
        return self.redis_mgr.register_queue(Queue(domain=domain))

    def create_proxy(self, address, port, protocol):
        proxy_keys = self.redis_mgr.redis.keys('p*')
        for pkey in proxy_keys:
            if self.redis_mgr.redis.hget(pkey,'address') == address and self.redis_mgr.redis.hget(pkey,'port'):
                logging.warn("Trying to create a proxy that already exists")
                return Proxy(**self.redis_mgr.redis.hgetall(pkey))

        proxy = Proxy(address=address,port=port,protocol=protocol)
        proxy = self.redis_mgr.register_proxy(proxy)
        proxy_key = proxy.proxy_key
        queue_key = "%s_%s" % ('q',SEED_QUEUE_ID)
        detail = Detail(proxy_key=proxy_key,queue_id=SEED_QUEUE_ID,queue_key=queue_key)
        
        
        self.redis_mgr.register_detail(detail)
        
        return proxy

    def clone_detail(self,detail,new_queue):

        new_queue_key = new_queue.queue_key
        
        if detail.queue_id != SEED_QUEUE_ID:
            raise Exception("can only clone details from seed queue")
        if not self.redis_mgr.redis.exists(new_queue_key):
            raise Exception("Invalid queue key while cloning detail")
        
        new_queue_id = self.redis_mgr.redis.hget(new_queue_key,'queue_id')
        proxy_id = detail.proxy_id
        proxy_key = detail.proxy_key
        lookup_key = 'd_%s_%s' % (new_queue.queue_key,detail.proxy_key)
        if self.redis_mgr.redis.exists(lookup_key):
            logging.warn("trying to clone a detail into a queue where it already exists.")
            return Detail(**self.redis_mgr.redis.hgetall(lookup_key))

        if new_queue.queue_id is not None and detail.proxy_id is not None:
            db_detail = self.db_mgr.get_detail_by_queue_and_proxy(new_queue_id,proxy_id)
            if db_detail is not None:
                logging.info("clone_detail: pulling detail from database instead")
                return self.redis_mgr.register_detail(db_detail)

        cloned = Detail(proxy_id=proxy_id,queue_id=new_queue_id,queue_key=new_queue_key, proxy_key=proxy_key)
        logging.info("created new unique detail ")
        self.redis_mgr.redis.sadd(NEW_DETAILS_SET_KEY,cloned.detail_key)
        return self.redis_mgr.register_detail(cloned)


    def sync_to_db(self):
        new_queues = [Queue(**self.redis_mgr.redis.hgetall(q)) for q in self.redis_mgr.redis.keys("qt_*")]
        new_proxies = [Proxy(**self.redis_mgr.redis.hgetall(p)) for p in self.redis_mgr.redis.keys("pt_*")]
        new_detail_keys = set(self.redis_mgr.redis.keys('d_qt*') + self.redis_mgr.redis.keys('d_*pt*'))
        for ndk in new_detail_keys:
            self.redis_mgr.redis.sadd(NEW_DETAILS_SET_KEY, ndk)
        
        new_details = [Detail(**self.redis_mgr.redis.hgetall(d)) for d in list(new_detail_keys)]

        cursor = self.db_mgr.cursor()

        queue_keys_to_id = {}
        proxy_keys_to_id = {}
        for q in new_queues:
            self.db_mgr.insert_queue(q,cursor)
            queue_id = cursor.fetchone()[0]
            queue_keys_to_id[q.queue_key] = queue_id

        for p in new_proxies:
            try:
                self.db_mgr.insert_proxy(p,cursor)
                proxy_id = cursor.fetchone()[0]
                proxy_keys_to_id[p.proxy_key] = proxy_id
            except psycopg2.errors.UniqueViolation as e:
                logging.warn("Duplicate proxy, fetch proxy id from database.")
                # existing_proxy = self.db_mgr.get_proxy_by_address_and_port(p.address,p.port)
                proxy_keys_to_id[p.proxy_key] = None


        for d in new_details:
            if d.proxy_id is None:
                new_proxy_id = proxy_keys_to_id[d.proxy_key]
                if new_proxy_id is None:
                    logging.warn("Discarding new detail, as it may already exist.")
                    continue
                else:
                    d.proxy_id = new_proxy_id
            if d.queue_id is None:
                d.queue_id = queue_keys_to_id[d.queue_key]
            self.db_mgr.insert_detail(d,cursor)
        

        changed_detail_keys = self.redis_mgr.redis.sdiff('changed_details','new_details')      
        changed_details = [Detail(**self.redis_mgr.redis.hgetall(d)) for d in self.redis_mgr.redis.sdiff('changed_details','new_details')]
        
        for changed in changed_details:
            if(changed.queue_id is None or changed.proxy_id is None):
                raise Exception("Unable to get a queue_id or proxy_id for an existing detail")
            
            self.db_mgr.update_detail(changed)
            
        logging.info("synced redis cache to database, resetting cache.")

        cursor.close()
        self.redis_mgr.redis.flushall()
        return True

        


        
        
        


