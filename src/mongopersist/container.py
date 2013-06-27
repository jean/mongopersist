##############################################################################
#
# Copyright (c) 2011 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""Mongo Container Implementations ala Zope Containers"""
import UserDict
import persistent
import bson.dbref
import bson.objectid
from rwproperty import getproperty, setproperty

import zope.component

from mongopersist import interfaces


class MongoContained(object):

    __parent__ = None
    __name__ = None

    _v_name = None
    _m_name_attr = None
    _m_name_getter = None
    _m_name_setter = None

    _m_parent_attr = None
    _m_parent_getter = None
    _m_parent_setter = None
    _v_parent = None

    @getproperty
    def __name__(self):
        if self._v_name is None:
            if self._m_name_attr is not None:
                self._v_name = getattr(self, self._m_name_attr, None)
            elif self._m_name_getter is not None:
                self._v_name = self._m_name_getter()
        return self._v_name
    @setproperty
    def __name__(self, value):
        if self._m_name_setter is not None:
            self._m_name_setter(value)
        self._v_name = value

    @getproperty
    def __parent__(self):
        if self._v_parent is None:
            if self._m_parent_attr is not None:
                self._v_parent = getattr(self, self._m_parent_attr, None)
            elif self._m_parent_getter is not None:
                self._v_parent = self._m_parent_getter()
        return self._v_parent
    @setproperty
    def __parent__(self, value):
        if self._m_parent_setter is not None:
            self._m_parent_setter(value)
        self._v_parent = value


class MongoContainer(persistent.Persistent,
                     UserDict.DictMixin):
    zope.interface.implements(interfaces.IMongoContainer)

    _m_database = None
    _m_collection = None
    _m_mapping_key = 'key'
    _m_parent_key = 'parent'
    _m_remove_documents = True

    def __init__(self, collection=None, database=None,
                 mapping_key=None, parent_key=None):
        if collection:
            self._m_collection = collection
        if database:
            self._m_database = database
        if mapping_key is not None:
            self._m_mapping_key = mapping_key
        if parent_key is not None:
            self._m_parent_key = parent_key

    @property
    def _m_jar(self):
        if not hasattr(self, '_v_mdmp'):
            # If the container is in a Mongo storage hierarchy, then getting
            # the datamanager is easy, otherwise we do an adapter lookup.
            if interfaces.IMongoDataManager.providedBy(self._p_jar):
                return self._p_jar

            # XXX: now what? zope.component requires zope.event
            # cache result of expensive component lookup
            self._v_mdmp = zope.component.getUtility(
                    interfaces.IMongoDataManagerProvider)

        return self._v_mdmp.get()

    def get_collection(self):
        db_name = self._m_database or self._m_jar.default_database
        return self._m_jar.get_collection(db_name, self._m_collection)

    def _m_get_parent_key_value(self):
        if getattr(self, '_p_jar', None) is None:
            raise ValueError('_p_jar not found.')
        if interfaces.IMongoDataManager.providedBy(self._p_jar):
            return self
        else:
            return 'zodb-'+''.join("%02x" % ord(x) for x in self._p_oid).strip()

    def _m_get_items_filter(self):
        filter = {}
        # Make sure that we only look through objects that have the mapping
        # key. Objects not having the mapping key cannot be part of the
        # collection.
        if self._m_mapping_key is not None:
            filter[self._m_mapping_key] = {'$exists': True}
        if self._m_parent_key is not None:
            gs = self._m_jar._writer.get_state
            filter[self._m_parent_key] = gs(self._m_get_parent_key_value())
        return filter

    def _m_add_items_filter(self, filter):
        for key, value in self._m_get_items_filter().items():
            if key not in filter:
                filter[key] = value

    def _locate(self, obj, doc):
        # Helper method that is only used when locating items that are already
        # in the container and are simply loaded from Mongo.
        if obj.__name__ is None:
            obj._v_name = doc[self._m_mapping_key]
        if obj.__parent__ is None:
            obj._v_parent = self

    def _load_one(self, doc):
        # Create a DBRef object and then load the full state of the object.
        dbref = bson.dbref.DBRef(
            self._m_collection, doc['_id'],
            self._m_database or self._m_jar.default_database)
        # Stick the doc into the _latest_states:
        self._m_jar._latest_states[dbref] = doc
        obj = self._m_jar.load(dbref)
        self._locate(obj, doc)
        return obj

    def __cmp__(self, other):
        # UserDict implements the semantics of implementing comparison of
        # items to determine equality, which is not what we want for a
        # container, so we revert back to the default object comparison.
        return cmp(id(self), id(other))

    def __getitem__(self, key):
        filter = self._m_get_items_filter()
        filter[self._m_mapping_key] = key
        obj = self.find_one(filter)
        if obj is None:
            raise KeyError(key)
        return obj

    def _real_setitem(self, key, value):
        # Make sure the value is in the database, since we might want
        # to use its oid.
        if value._p_oid is None:
            self._m_jar.insert(value)

        # This call by itself causes the state to change _p_changed to True.
        if self._m_mapping_key is not None:
            setattr(value, self._m_mapping_key, key)
        if self._m_parent_key is not None:
            setattr(value, self._m_parent_key, self._m_get_parent_key_value())

    def __setitem__(self, key, value):
        # When the key is None, we need to determine it.
        if key is None:
            if self._m_mapping_key is None:
                # Make sure the value is in the database, since we might want
                # to use its oid.
                if value._p_oid is None:
                    self._m_jar.insert(value)
                key = unicode(value._p_oid.id)
            else:
                # we have _m_mapping_key, use that attribute
                key = getattr(value, self._m_mapping_key)
        # hook to allow firing events with zope.container.MongoContainer
        self._after_setitem_hook(key, value)

    def _after_setitem_hook(self, key, value):
        # this is a copy of zope.container.contained.setitem without the events
        # Do basic key check:
        if isinstance(key, str):
            try:
                key = unicode(key)
            except UnicodeError:
                raise TypeError("key not unicode or ascii string")
        elif not isinstance(key, unicode):
            raise TypeError("key not unicode or ascii string")

        if not key:
            raise ValueError("empty keys are not allowed")

        old = self.get(key)
        if old is value:
            # the container already has the same item
            return
        if old is not None:
            # the container has an item with the same key, but different obj
            raise KeyError(key)

        if value.__parent__ is self and value.__name__ == key:
            pass
        else:
            value.__parent__ = self
            value.__name__ = key

        self._real_setitem(key, value)

    def add(self, value, key=None):
        # We are already supporting ``None`` valued keys, which prompts the key
        # to be determined here. But people felt that a more explicit
        # interface would be better in this case.
        self[key] = value

    def __delitem__(self, key):
        value = self[key]
        # First remove the parent and name from the object.
        if self._m_mapping_key is not None:
            try:
                delattr(value, self._m_mapping_key)
            except AttributeError:
                # Sometimes we do not control those attributes.
                pass
        if self._m_parent_key is not None:
            try:
                delattr(value, self._m_parent_key)
            except AttributeError:
                # Sometimes we do not control those attributes.
                pass
        # Let's now remove the object from the database.
        if self._m_remove_documents:
            self._m_jar.remove(value)

        self._after_delitem_hook(key, value)

    def _after_delitem_hook(self, key, value):
        value.__parent__ = None
        value.__name__ = None

    def __contains__(self, key):
        return self.raw_find_one(
            {self._m_mapping_key: key}, fields=()) is not None

    def __iter__(self):
        result = self.raw_find(
            {self._m_mapping_key: {'$ne': None}}, fields=(self._m_mapping_key,))
        for doc in result:
            yield doc[self._m_mapping_key]

    def keys(self):
        return list(self.__iter__())

    def iteritems(self):
        result = self.raw_find()
        for doc in result:
            obj = self._load_one(doc)
            yield doc[self._m_mapping_key], obj

    def raw_find(self, spec=None, *args, **kwargs):
        if spec is None:
            spec = {}
        self._m_add_items_filter(spec)
        coll = self.get_collection()
        return coll.find(spec, *args, **kwargs)

    def find(self, spec=None, *args, **kwargs):
        # Search for matching objects.
        result = self.raw_find(spec, *args, **kwargs)
        for doc in result:
            obj = self._load_one(doc)
            yield obj

    def raw_find_one(self, spec_or_id=None, *args, **kwargs):
        if spec_or_id is None:
            spec_or_id = {}
        if not isinstance(spec_or_id, dict):
            spec_or_id = {'_id': spec_or_id}
        self._m_add_items_filter(spec_or_id)
        coll = self.get_collection()
        return coll.find_one(spec_or_id, *args, **kwargs)

    def find_one(self, spec_or_id=None, *args, **kwargs):
        doc = self.raw_find_one(spec_or_id, *args, **kwargs)
        if doc is None:
            return None
        return self._load_one(doc)


class AllItemsMongoContainer(MongoContainer):
    _m_parent_key = None


class SubDocumentMongoContainer(MongoContained, MongoContainer):
    _p_mongo_sub_object = True
