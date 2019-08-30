# -*- coding: utf-8 -*-
import psycopg2
import uuid

from odoo.tests import common


class TestLocalPg(common.TransactionCase):

    def setUp(self):
        super(TestLocalPg, self).setUp()
        self.Build = self.env['runbot.build']

    def test_build_local_pg(self):
            """ test create and drop of a local database even with an open cursor"""
            dbname = str(uuid.uuid4())
            self.Build._local_pg_createdb(dbname)
            self.Build._local_pg_limit_db(dbname, 2)
            cnx = psycopg2.connect("dbname=%s" % dbname)
            cur = cnx.cursor()
            cur.execute("CREATE TABLE test (id serial PRIMARY KEY, foo varchar);")
            cur.execute("INSERT INTO test (foo) VALUES ('bar')")
            failure = False
            try:
                self.Build._local_pg_dropdb(dbname)
            except psycopg2.OperationalError:
                cnx.close()
                self.Build._local_pg_dropdb(dbname)
                failure = True
            self.assertFalse(failure, "_local_pg_dropdb failed when a cursor is still using DB")
