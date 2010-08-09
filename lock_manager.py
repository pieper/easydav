# -*- coding: utf-8 -*-

'''Stores lock data in a SQLite database. SQLite handles the cross-process
and cross-thread synchronization, while this code handles WebDAV lock semantics.
'''

from __future__ import with_statement
import webdavconfig as config
import os.path
import davutils
import sqlite3
from uuid import uuid4
import datetime
import time

class Lock:
    def __init__(self, row):
        self.urn = row[0]
        self.path = row[1]
        self.shared = row[2]
        self.owner = row[3]
        self.infinite_depth = row[4]
        self.timeout = row[5]
    
    def __eq__(self, other):
        return isinstance(other, Lock) and other.urn == self.urn

class LockManager:
    def __init__(self):
        # Lock_db can be absolute path or relative to root dir.
        dbpath = os.path.join(config.root_dir, config.lock_db)
        newfile = not os.path.exists(dbpath)
        
        # Default timeout for SQL locks is 5 seconds
        self.db_conn = sqlite3.connect(dbpath)
        self.db_conn.row_factory = sqlite3.Row
        self.db_cursor = self.db_conn.cursor()
        
        if newfile:
            self.create_tables()
        else:
            self._purge_locks()
            self.db_conn.commit()
    
    def _create_tables(self):
        self.db_cursor.execute('''CREATE TABLE locks (
            urn TEXT,
            path TEXT,
            shared BOOLEAN,
            owner TEXT,
            infinite_depth BOOLEAN,
            valid_until TIMESTAMP)''')
        self.db_conn.commit()
    
    def _purge_locks(self):
        '''Remove all expired locks from the database.'''
        self.db_cursor.execute('''DELETE FROM locks WHERE
            valid_until < DATETIME('now')''')
    
    def get_locks(self, real_path):
        '''Returns all locks that apply to the resource defined by real_path.
        Result is a list of Lock objects.
        '''
        rel_path = davutils.get_relpath(real_path, config.root_dir)
        
        path_exprs = ['path = ?']
        path_args = [rel_path]
        
        # Construct a list of parent directories that have to be checked
        # for locks.
        while rel_path:
            rel_path = os.path.dirname(rel_path)
            path_exprs.append('(infinite_depth AND path = ?)')
            path_args.append(rel_path)

        self.db_cursor.execute('SELECT * FROM locks WHERE '
            + ' OR '.join(path_exprs), path_args)
        return map(Lock, self.db_cursor.fetchall())
    
    def lock_path(self, real_path, shared, owner, depth, timeout):
        '''Create a lock for the resource defined by real_path. Arguments
        are as follows:
        real_path: full path to the resource in local file system
        shared: True for shared lock, False for exclusive lock
        owner: client-provided XML string describing the owner of the lock
        depth: -1 for infinite, 0 otherwise
        timeout: Client-requested lock expiration time in seconds from now.
                 Configuration may limit actual timeout.
        '''
        assert depth in [-1, 0]
        
        uuid = uuid4()
        rel_path = davutils.get_relpath(real_path, config.root_dir)
        
        timeout = min(timeout, config.lock_max_time)
        valid_until = datetime.datetime.now()
        valid_until += datetime.timedelta(seconds = timeout)
        
        row = [uuid, rel_path, shared, owner, depth == -1, valid_until]

        self.db_cursor.execute('BEGIN IMMEDIATE TRANSACTION')
        
        
