# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import OrderedDict, deque, namedtuple
from functools import wraps
import itertools
import logging

import simplejson as json
from twisted.internet import defer


from synapse.storage.events_worker import EventsWorkerStore
from synapse.util.async import ObservableDeferred
from synapse.util.frozenutils import frozendict_json_encoder
from synapse.util.logcontext import (
    PreserveLoggingContext, make_deferred_yieldable,
)
from synapse.util.logutils import log_function
from synapse.util.metrics import Measure
from synapse.api.constants import EventTypes
from synapse.api.errors import SynapseError
from synapse.util.caches.descriptors import cached, cachedInlineCallbacks
from synapse.types import get_domain_from_id
import synapse.metrics

# these are only included to make the type annotations work
from synapse.events import EventBase    # noqa: F401
from synapse.events.snapshot import EventContext   # noqa: F401

logger = logging.getLogger(__name__)


metrics = synapse.metrics.get_metrics_for(__name__)
persist_event_counter = metrics.register_counter("persisted_events")
event_counter = metrics.register_counter(
    "persisted_events_sep", labels=["type", "origin_type", "origin_entity"]
)

# The number of times we are recalculating the current state
state_delta_counter = metrics.register_counter(
    "state_delta",
)
# The number of times we are recalculating state when there is only a
# single forward extremity
state_delta_single_event_counter = metrics.register_counter(
    "state_delta_single_event",
)
# The number of times we are reculating state when we could have resonably
# calculated the delta when we calculated the state for an event we were
# persisting.
state_delta_reuse_delta_counter = metrics.register_counter(
    "state_delta_reuse_delta",
)


def encode_json(json_object):
    return frozendict_json_encoder.encode(json_object)


class _EventPeristenceQueue(object):
    """Queues up events so that they can be persisted in bulk with only one
    concurrent transaction per room.
    """

    _EventPersistQueueItem = namedtuple("_EventPersistQueueItem", (
        "events_and_contexts", "backfilled", "deferred",
    ))

    def __init__(self):
        self._event_persist_queues = {}
        self._currently_persisting_rooms = set()

    def add_to_queue(self, room_id, events_and_contexts, backfilled):
        """Add events to the queue, with the given persist_event options.

        NB: due to the normal usage pattern of this method, it does *not*
        follow the synapse logcontext rules, and leaves the logcontext in
        place whether or not the returned deferred is ready.

        Args:
            room_id (str):
            events_and_contexts (list[(EventBase, EventContext)]):
            backfilled (bool):

        Returns:
            defer.Deferred: a deferred which will resolve once the events are
                persisted. Runs its callbacks *without* a logcontext.
        """
        queue = self._event_persist_queues.setdefault(room_id, deque())
        if queue:
            # if the last item in the queue has the same `backfilled` setting,
            # we can just add these new events to that item.
            end_item = queue[-1]
            if end_item.backfilled == backfilled:
                end_item.events_and_contexts.extend(events_and_contexts)
                return end_item.deferred.observe()

        deferred = ObservableDeferred(defer.Deferred(), consumeErrors=True)

        queue.append(self._EventPersistQueueItem(
            events_and_contexts=events_and_contexts,
            backfilled=backfilled,
            deferred=deferred,
        ))

        return deferred.observe()

    def handle_queue(self, room_id, per_item_callback):
        """Attempts to handle the queue for a room if not already being handled.

        The given callback will be invoked with for each item in the queue,
        of type _EventPersistQueueItem. The per_item_callback will continuously
        be called with new items, unless the queue becomnes empty. The return
        value of the function will be given to the deferreds waiting on the item,
        exceptions will be passed to the deferreds as well.

        This function should therefore be called whenever anything is added
        to the queue.

        If another callback is currently handling the queue then it will not be
        invoked.
        """

        if room_id in self._currently_persisting_rooms:
            return

        self._currently_persisting_rooms.add(room_id)

        @defer.inlineCallbacks
        def handle_queue_loop():
            try:
                queue = self._get_drainining_queue(room_id)
                for item in queue:
                    # handle_queue_loop runs in the sentinel logcontext, so
                    # there is no need to preserve_fn when running the
                    # callbacks on the deferred.
                    try:
                        ret = yield per_item_callback(item)
                        item.deferred.callback(ret)
                    except Exception:
                        item.deferred.errback()
            finally:
                queue = self._event_persist_queues.pop(room_id, None)
                if queue:
                    self._event_persist_queues[room_id] = queue
                self._currently_persisting_rooms.discard(room_id)

        # set handle_queue_loop off on the background. We don't want to
        # attribute work done in it to the current request, so we drop the
        # logcontext altogether.
        with PreserveLoggingContext():
            handle_queue_loop()

    def _get_drainining_queue(self, room_id):
        queue = self._event_persist_queues.setdefault(room_id, deque())

        try:
            while True:
                yield queue.popleft()
        except IndexError:
            # Queue has been drained.
            pass


_EventCacheEntry = namedtuple("_EventCacheEntry", ("event", "redacted_event"))


def _retry_on_integrity_error(func):
    """Wraps a database function so that it gets retried on IntegrityError,
    with `delete_existing=True` passed in.

    Args:
        func: function that returns a Deferred and accepts a `delete_existing` arg
    """
    @wraps(func)
    @defer.inlineCallbacks
    def f(self, *args, **kwargs):
        try:
            res = yield func(self, *args, **kwargs)
        except self.database_engine.module.IntegrityError:
            logger.exception("IntegrityError, retrying.")
            res = yield func(self, *args, delete_existing=True, **kwargs)
        defer.returnValue(res)

    return f


class EventsStore(EventsWorkerStore):
    EVENT_ORIGIN_SERVER_TS_NAME = "event_origin_server_ts"
    EVENT_FIELDS_SENDER_URL_UPDATE_NAME = "event_fields_sender_url"

    def __init__(self, db_conn, hs):
        super(EventsStore, self).__init__(db_conn, hs)
        self.register_background_update_handler(
            self.EVENT_ORIGIN_SERVER_TS_NAME, self._background_reindex_origin_server_ts
        )
        self.register_background_update_handler(
            self.EVENT_FIELDS_SENDER_URL_UPDATE_NAME,
            self._background_reindex_fields_sender,
        )

        self.register_background_index_update(
            "event_contains_url_index",
            index_name="event_contains_url_index",
            table="events",
            columns=["room_id", "topological_ordering", "stream_ordering"],
            where_clause="contains_url = true AND outlier = false",
        )

        # an event_id index on event_search is useful for the purge_history
        # api. Plus it means we get to enforce some integrity with a UNIQUE
        # clause
        self.register_background_index_update(
            "event_search_event_id_idx",
            index_name="event_search_event_id_idx",
            table="event_search",
            columns=["event_id"],
            unique=True,
            psql_only=True,
        )

        self._event_persist_queue = _EventPeristenceQueue()

        self._state_resolution_handler = hs.get_state_resolution_handler()

    def persist_events(self, events_and_contexts, backfilled=False):
        """
        Write events to the database
        Args:
            events_and_contexts: list of tuples of (event, context)
            backfilled: ?
        """
        partitioned = {}
        for event, ctx in events_and_contexts:
            partitioned.setdefault(event.room_id, []).append((event, ctx))

        deferreds = []
        for room_id, evs_ctxs in partitioned.iteritems():
            d = self._event_persist_queue.add_to_queue(
                room_id, evs_ctxs,
                backfilled=backfilled,
            )
            deferreds.append(d)

        for room_id in partitioned:
            self._maybe_start_persisting(room_id)

        return make_deferred_yieldable(
            defer.gatherResults(deferreds, consumeErrors=True)
        )

    @defer.inlineCallbacks
    @log_function
    def persist_event(self, event, context, backfilled=False):
        """

        Args:
            event (EventBase):
            context (EventContext):
            backfilled (bool):

        Returns:
            Deferred: resolves to (int, int): the stream ordering of ``event``,
            and the stream ordering of the latest persisted event
        """
        deferred = self._event_persist_queue.add_to_queue(
            event.room_id, [(event, context)],
            backfilled=backfilled,
        )

        self._maybe_start_persisting(event.room_id)

        yield make_deferred_yieldable(deferred)

        max_persisted_id = yield self._stream_id_gen.get_current_token()
        defer.returnValue((event.internal_metadata.stream_ordering, max_persisted_id))

    def _maybe_start_persisting(self, room_id):
        @defer.inlineCallbacks
        def persisting_queue(item):
            with Measure(self._clock, "persist_events"):
                yield self._persist_events(
                    item.events_and_contexts,
                    backfilled=item.backfilled,
                )

        self._event_persist_queue.handle_queue(room_id, persisting_queue)

    @_retry_on_integrity_error
    @defer.inlineCallbacks
    def _persist_events(self, events_and_contexts, backfilled=False,
                        delete_existing=False):
        """Persist events to db

        Args:
            events_and_contexts (list[(EventBase, EventContext)]):
            backfilled (bool):
            delete_existing (bool):

        Returns:
            Deferred: resolves when the events have been persisted
        """
        if not events_and_contexts:
            return

        if backfilled:
            stream_ordering_manager = self._backfill_id_gen.get_next_mult(
                len(events_and_contexts)
            )
        else:
            stream_ordering_manager = self._stream_id_gen.get_next_mult(
                len(events_and_contexts)
            )

        with stream_ordering_manager as stream_orderings:
            for (event, context), stream, in zip(
                events_and_contexts, stream_orderings
            ):
                event.internal_metadata.stream_ordering = stream

            chunks = [
                events_and_contexts[x:x + 100]
                for x in xrange(0, len(events_and_contexts), 100)
            ]

            for chunk in chunks:
                # We can't easily parallelize these since different chunks
                # might contain the same event. :(

                # NB: Assumes that we are only persisting events for one room
                # at a time.

                # map room_id->list[event_ids] giving the new forward
                # extremities in each room
                new_forward_extremeties = {}

                # map room_id->(type,state_key)->event_id tracking the full
                # state in each room after adding these events
                current_state_for_room = {}

                # map room_id->(to_delete, to_insert) where each entry is
                # a map (type,key)->event_id giving the state delta in each
                # room
                state_delta_for_room = {}

                if not backfilled:
                    with Measure(self._clock, "_calculate_state_and_extrem"):
                        # Work out the new "current state" for each room.
                        # We do this by working out what the new extremities are and then
                        # calculating the state from that.
                        events_by_room = {}
                        for event, context in chunk:
                            events_by_room.setdefault(event.room_id, []).append(
                                (event, context)
                            )

                        for room_id, ev_ctx_rm in events_by_room.iteritems():
                            # Work out new extremities by recursively adding and removing
                            # the new events.
                            latest_event_ids = yield self.get_latest_event_ids_in_room(
                                room_id
                            )
                            new_latest_event_ids = yield self._calculate_new_extremeties(
                                room_id, ev_ctx_rm, latest_event_ids
                            )

                            latest_event_ids = set(latest_event_ids)
                            if new_latest_event_ids == latest_event_ids:
                                # No change in extremities, so no change in state
                                continue

                            new_forward_extremeties[room_id] = new_latest_event_ids

                            len_1 = (
                                len(latest_event_ids) == 1
                                and len(new_latest_event_ids) == 1
                            )
                            if len_1:
                                all_single_prev_not_state = all(
                                    len(event.prev_events) == 1
                                    and not event.is_state()
                                    for event, ctx in ev_ctx_rm
                                )
                                # Don't bother calculating state if they're just
                                # a long chain of single ancestor non-state events.
                                if all_single_prev_not_state:
                                    continue

                            state_delta_counter.inc()
                            if len(new_latest_event_ids) == 1:
                                state_delta_single_event_counter.inc()

                                # This is a fairly handwavey check to see if we could
                                # have guessed what the delta would have been when
                                # processing one of these events.
                                # What we're interested in is if the latest extremities
                                # were the same when we created the event as they are
                                # now. When this server creates a new event (as opposed
                                # to receiving it over federation) it will use the
                                # forward extremities as the prev_events, so we can
                                # guess this by looking at the prev_events and checking
                                # if they match the current forward extremities.
                                for ev, _ in ev_ctx_rm:
                                    prev_event_ids = set(e for e, _ in ev.prev_events)
                                    if latest_event_ids == prev_event_ids:
                                        state_delta_reuse_delta_counter.inc()
                                        break

                            logger.info(
                                "Calculating state delta for room %s", room_id,
                            )
                            current_state = yield self._get_new_state_after_events(
                                room_id,
                                ev_ctx_rm, new_latest_event_ids,
                            )
                            if current_state is not None:
                                current_state_for_room[room_id] = current_state
                                delta = yield self._calculate_state_delta(
                                    room_id, current_state,
                                )
                                if delta is not None:
                                    state_delta_for_room[room_id] = delta

                yield self.runInteraction(
                    "persist_events",
                    self._persist_events_txn,
                    events_and_contexts=chunk,
                    backfilled=backfilled,
                    delete_existing=delete_existing,
                    state_delta_for_room=state_delta_for_room,
                    new_forward_extremeties=new_forward_extremeties,
                )
                persist_event_counter.inc_by(len(chunk))
                synapse.metrics.event_persisted_position.set(
                    chunk[-1][0].internal_metadata.stream_ordering,
                )
                for event, context in chunk:
                    if context.app_service:
                        origin_type = "local"
                        origin_entity = context.app_service.id
                    elif self.hs.is_mine_id(event.sender):
                        origin_type = "local"
                        origin_entity = "*client*"
                    else:
                        origin_type = "remote"
                        origin_entity = get_domain_from_id(event.sender)

                    event_counter.inc(event.type, origin_type, origin_entity)

                for room_id, new_state in current_state_for_room.iteritems():
                    self.get_current_state_ids.prefill(
                        (room_id, ), new_state
                    )

                for room_id, latest_event_ids in new_forward_extremeties.iteritems():
                    self.get_latest_event_ids_in_room.prefill(
                        (room_id,), list(latest_event_ids)
                    )

    @defer.inlineCallbacks
    def _calculate_new_extremeties(self, room_id, event_contexts, latest_event_ids):
        """Calculates the new forward extremeties for a room given events to
        persist.

        Assumes that we are only persisting events for one room at a time.
        """
        new_latest_event_ids = set(latest_event_ids)
        # First, add all the new events to the list
        new_latest_event_ids.update(
            event.event_id for event, ctx in event_contexts
            if not event.internal_metadata.is_outlier() and not ctx.rejected
        )
        # Now remove all events that are referenced by the to-be-added events
        new_latest_event_ids.difference_update(
            e_id
            for event, ctx in event_contexts
            for e_id, _ in event.prev_events
            if not event.internal_metadata.is_outlier() and not ctx.rejected
        )

        # And finally remove any events that are referenced by previously added
        # events.
        rows = yield self._simple_select_many_batch(
            table="event_edges",
            column="prev_event_id",
            iterable=list(new_latest_event_ids),
            retcols=["prev_event_id"],
            keyvalues={
                "room_id": room_id,
                "is_state": False,
            },
            desc="_calculate_new_extremeties",
        )

        new_latest_event_ids.difference_update(
            row["prev_event_id"] for row in rows
        )

        defer.returnValue(new_latest_event_ids)

    @defer.inlineCallbacks
    def _get_new_state_after_events(self, room_id, events_context, new_latest_event_ids):
        """Calculate the current state dict after adding some new events to
        a room

        Args:
            room_id (str):
                room to which the events are being added. Used for logging etc

            events_context (list[(EventBase, EventContext)]):
                events and contexts which are being added to the room

            new_latest_event_ids (iterable[str]):
                the new forward extremities for the room.

        Returns:
            Deferred[dict[(str,str), str]|None]:
                None if there are no changes to the room state, or
                a dict of (type, state_key) -> event_id].
        """

        if not new_latest_event_ids:
            defer.returnValue({})

        # map from state_group to ((type, key) -> event_id) state map
        state_groups = {}
        missing_event_ids = []
        was_updated = False
        for event_id in new_latest_event_ids:
            # First search in the list of new events we're adding,
            # and then use the current state from that
            for ev, ctx in events_context:
                if event_id == ev.event_id:
                    if ctx.current_state_ids is None:
                        raise Exception("Unknown current state")

                    if ctx.state_group is None:
                        # I don't think this can happen, but let's double-check
                        raise Exception(
                            "Context for new extremity event %s has no state "
                            "group" % (event_id, ),
                        )

                    # If we've already seen the state group don't bother adding
                    # it to the state sets again
                    if ctx.state_group not in state_groups:
                        state_groups[ctx.state_group] = ctx.current_state_ids
                        if ctx.delta_ids or hasattr(ev, "state_key"):
                            was_updated = True
                    break
            else:
                # If we couldn't find it, then we'll need to pull
                # the state from the database
                was_updated = True
                missing_event_ids.append(event_id)

        if not was_updated:
            return

        if missing_event_ids:
            # Now pull out the state for any missing events from DB
            event_to_groups = yield self._get_state_group_for_events(
                missing_event_ids,
            )

            groups = set(event_to_groups.itervalues()) - set(state_groups.iterkeys())

            if groups:
                group_to_state = yield self._get_state_for_groups(groups)
                state_groups.update(group_to_state)

        if len(state_groups) == 1:
            # If there is only one state group, then we know what the current
            # state is.
            defer.returnValue(state_groups.values()[0])

        def get_events(ev_ids):
            return self.get_events(
                ev_ids, get_prev_content=False, check_redacted=False,
            )
        events_map = {ev.event_id: ev for ev, _ in events_context}
        logger.debug("calling resolve_state_groups from preserve_events")
        res = yield self._state_resolution_handler.resolve_state_groups(
            room_id, state_groups, events_map, get_events
        )

        defer.returnValue(res.state)

    @defer.inlineCallbacks
    def _calculate_state_delta(self, room_id, current_state):
        """Calculate the new state deltas for a room.

        Assumes that we are only persisting events for one room at a time.

        Returns:
            2-tuple (to_delete, to_insert) where both are state dicts,
            i.e. (type, state_key) -> event_id. `to_delete` are the entries to
            first be deleted from current_state_events, `to_insert` are entries
            to insert.
        """
        existing_state = yield self.get_current_state_ids(room_id)

        existing_events = set(existing_state.itervalues())
        new_events = set(ev_id for ev_id in current_state.itervalues())
        changed_events = existing_events ^ new_events

        if not changed_events:
            return

        to_delete = {
            key: ev_id for key, ev_id in existing_state.iteritems()
            if ev_id in changed_events
        }
        events_to_insert = (new_events - existing_events)
        to_insert = {
            key: ev_id for key, ev_id in current_state.iteritems()
            if ev_id in events_to_insert
        }

        defer.returnValue((to_delete, to_insert))

    @log_function
    def _persist_events_txn(self, txn, events_and_contexts, backfilled,
                            delete_existing=False, state_delta_for_room={},
                            new_forward_extremeties={}):
        """Insert some number of room events into the necessary database tables.

        Rejected events are only inserted into the events table, the events_json table,
        and the rejections table. Things reading from those table will need to check
        whether the event was rejected.

        Args:
            txn (twisted.enterprise.adbapi.Connection): db connection
            events_and_contexts (list[(EventBase, EventContext)]):
                events to persist
            backfilled (bool): True if the events were backfilled
            delete_existing (bool): True to purge existing table rows for the
                events from the database. This is useful when retrying due to
                IntegrityError.
            state_delta_for_room (dict[str, (list[str], list[str])]):
                The current-state delta for each room. For each room, a tuple
                (to_delete, to_insert), being a list of event ids to be removed
                from the current state, and a list of event ids to be added to
                the current state.
            new_forward_extremeties (dict[str, list[str]]):
                The new forward extremities for each room. For each room, a
                list of the event ids which are the forward extremities.

        """
        all_events_and_contexts = events_and_contexts

        max_stream_order = events_and_contexts[-1][0].internal_metadata.stream_ordering

        self._update_current_state_txn(txn, state_delta_for_room, max_stream_order)

        self._update_forward_extremities_txn(
            txn,
            new_forward_extremities=new_forward_extremeties,
            max_stream_order=max_stream_order,
        )

        # Ensure that we don't have the same event twice.
        events_and_contexts = self._filter_events_and_contexts_for_duplicates(
            events_and_contexts,
        )

        self._update_room_depths_txn(
            txn,
            events_and_contexts=events_and_contexts,
            backfilled=backfilled,
        )

        # _update_outliers_txn filters out any events which have already been
        # persisted, and returns the filtered list.
        events_and_contexts = self._update_outliers_txn(
            txn,
            events_and_contexts=events_and_contexts,
        )

        # From this point onwards the events are only events that we haven't
        # seen before.

        if delete_existing:
            # For paranoia reasons, we go and delete all the existing entries
            # for these events so we can reinsert them.
            # This gets around any problems with some tables already having
            # entries.
            self._delete_existing_rows_txn(
                txn,
                events_and_contexts=events_and_contexts,
            )

        self._store_event_txn(
            txn,
            events_and_contexts=events_and_contexts,
        )

        # Insert into event_to_state_groups.
        self._store_event_state_mappings_txn(txn, events_and_contexts)

        # _store_rejected_events_txn filters out any events which were
        # rejected, and returns the filtered list.
        events_and_contexts = self._store_rejected_events_txn(
            txn,
            events_and_contexts=events_and_contexts,
        )

        # From this point onwards the events are only ones that weren't
        # rejected.

        self._update_metadata_tables_txn(
            txn,
            events_and_contexts=events_and_contexts,
            all_events_and_contexts=all_events_and_contexts,
            backfilled=backfilled,
        )

    def _update_current_state_txn(self, txn, state_delta_by_room, max_stream_order):
        for room_id, current_state_tuple in state_delta_by_room.iteritems():
                to_delete, to_insert = current_state_tuple
                txn.executemany(
                    "DELETE FROM current_state_events WHERE event_id = ?",
                    [(ev_id,) for ev_id in to_delete.itervalues()],
                )

                self._simple_insert_many_txn(
                    txn,
                    table="current_state_events",
                    values=[
                        {
                            "event_id": ev_id,
                            "room_id": room_id,
                            "type": key[0],
                            "state_key": key[1],
                        }
                        for key, ev_id in to_insert.iteritems()
                    ],
                )

                state_deltas = {key: None for key in to_delete}
                state_deltas.update(to_insert)

                self._simple_insert_many_txn(
                    txn,
                    table="current_state_delta_stream",
                    values=[
                        {
                            "stream_id": max_stream_order,
                            "room_id": room_id,
                            "type": key[0],
                            "state_key": key[1],
                            "event_id": ev_id,
                            "prev_event_id": to_delete.get(key, None),
                        }
                        for key, ev_id in state_deltas.iteritems()
                    ]
                )

                self._curr_state_delta_stream_cache.entity_has_changed(
                    room_id, max_stream_order,
                )

                # Invalidate the various caches

                # Figure out the changes of membership to invalidate the
                # `get_rooms_for_user` cache.
                # We find out which membership events we may have deleted
                # and which we have added, then we invlidate the caches for all
                # those users.
                members_changed = set(
                    state_key for ev_type, state_key in state_deltas
                    if ev_type == EventTypes.Member
                )

                for member in members_changed:
                    self._invalidate_cache_and_stream(
                        txn, self.get_rooms_for_user_with_stream_ordering, (member,)
                    )

                for host in set(get_domain_from_id(u) for u in members_changed):
                    self._invalidate_cache_and_stream(
                        txn, self.is_host_joined, (room_id, host)
                    )
                    self._invalidate_cache_and_stream(
                        txn, self.was_host_joined, (room_id, host)
                    )

                self._invalidate_cache_and_stream(
                    txn, self.get_users_in_room, (room_id,)
                )

                self._invalidate_cache_and_stream(
                    txn, self.get_current_state_ids, (room_id,)
                )

    def _update_forward_extremities_txn(self, txn, new_forward_extremities,
                                        max_stream_order):
        for room_id, new_extrem in new_forward_extremities.iteritems():
            self._simple_delete_txn(
                txn,
                table="event_forward_extremities",
                keyvalues={"room_id": room_id},
            )
            txn.call_after(
                self.get_latest_event_ids_in_room.invalidate, (room_id,)
            )

        self._simple_insert_many_txn(
            txn,
            table="event_forward_extremities",
            values=[
                {
                    "event_id": ev_id,
                    "room_id": room_id,
                }
                for room_id, new_extrem in new_forward_extremities.iteritems()
                for ev_id in new_extrem
            ],
        )
        # We now insert into stream_ordering_to_exterm a mapping from room_id,
        # new stream_ordering to new forward extremeties in the room.
        # This allows us to later efficiently look up the forward extremeties
        # for a room before a given stream_ordering
        self._simple_insert_many_txn(
            txn,
            table="stream_ordering_to_exterm",
            values=[
                {
                    "room_id": room_id,
                    "event_id": event_id,
                    "stream_ordering": max_stream_order,
                }
                for room_id, new_extrem in new_forward_extremities.iteritems()
                for event_id in new_extrem
            ]
        )

    @classmethod
    def _filter_events_and_contexts_for_duplicates(cls, events_and_contexts):
        """Ensure that we don't have the same event twice.

        Pick the earliest non-outlier if there is one, else the earliest one.

        Args:
            events_and_contexts (list[(EventBase, EventContext)]):
        Returns:
            list[(EventBase, EventContext)]: filtered list
        """
        new_events_and_contexts = OrderedDict()
        for event, context in events_and_contexts:
            prev_event_context = new_events_and_contexts.get(event.event_id)
            if prev_event_context:
                if not event.internal_metadata.is_outlier():
                    if prev_event_context[0].internal_metadata.is_outlier():
                        # To ensure correct ordering we pop, as OrderedDict is
                        # ordered by first insertion.
                        new_events_and_contexts.pop(event.event_id, None)
                        new_events_and_contexts[event.event_id] = (event, context)
            else:
                new_events_and_contexts[event.event_id] = (event, context)
        return new_events_and_contexts.values()

    def _update_room_depths_txn(self, txn, events_and_contexts, backfilled):
        """Update min_depth for each room

        Args:
            txn (twisted.enterprise.adbapi.Connection): db connection
            events_and_contexts (list[(EventBase, EventContext)]): events
                we are persisting
            backfilled (bool): True if the events were backfilled
        """
        depth_updates = {}
        for event, context in events_and_contexts:
            # Remove the any existing cache entries for the event_ids
            txn.call_after(self._invalidate_get_event_cache, event.event_id)
            if not backfilled:
                txn.call_after(
                    self._events_stream_cache.entity_has_changed,
                    event.room_id, event.internal_metadata.stream_ordering,
                )

            if not event.internal_metadata.is_outlier() and not context.rejected:
                depth_updates[event.room_id] = max(
                    event.depth, depth_updates.get(event.room_id, event.depth)
                )

        for room_id, depth in depth_updates.iteritems():
            self._update_min_depth_for_room_txn(txn, room_id, depth)

    def _update_outliers_txn(self, txn, events_and_contexts):
        """Update any outliers with new event info.

        This turns outliers into ex-outliers (unless the new event was
        rejected).

        Args:
            txn (twisted.enterprise.adbapi.Connection): db connection
            events_and_contexts (list[(EventBase, EventContext)]): events
                we are persisting

        Returns:
            list[(EventBase, EventContext)] new list, without events which
            are already in the events table.
        """
        txn.execute(
            "SELECT event_id, outlier FROM events WHERE event_id in (%s)" % (
                ",".join(["?"] * len(events_and_contexts)),
            ),
            [event.event_id for event, _ in events_and_contexts]
        )

        have_persisted = {
            event_id: outlier
            for event_id, outlier in txn
        }

        to_remove = set()
        for event, context in events_and_contexts:
            if event.event_id not in have_persisted:
                continue

            to_remove.add(event)

            if context.rejected:
                # If the event is rejected then we don't care if the event
                # was an outlier or not.
                continue

            outlier_persisted = have_persisted[event.event_id]
            if not event.internal_metadata.is_outlier() and outlier_persisted:
                # We received a copy of an event that we had already stored as
                # an outlier in the database. We now have some state at that
                # so we need to update the state_groups table with that state.

                # insert into event_to_state_groups.
                try:
                    self._store_event_state_mappings_txn(txn, ((event, context),))
                except Exception:
                    logger.exception("")
                    raise

                metadata_json = encode_json(
                    event.internal_metadata.get_dict()
                ).decode("UTF-8")

                sql = (
                    "UPDATE event_json SET internal_metadata = ?"
                    " WHERE event_id = ?"
                )
                txn.execute(
                    sql,
                    (metadata_json, event.event_id,)
                )

                # Add an entry to the ex_outlier_stream table to replicate the
                # change in outlier status to our workers.
                stream_order = event.internal_metadata.stream_ordering
                state_group_id = context.state_group
                self._simple_insert_txn(
                    txn,
                    table="ex_outlier_stream",
                    values={
                        "event_stream_ordering": stream_order,
                        "event_id": event.event_id,
                        "state_group": state_group_id,
                    }
                )

                sql = (
                    "UPDATE events SET outlier = ?"
                    " WHERE event_id = ?"
                )
                txn.execute(
                    sql,
                    (False, event.event_id,)
                )

                # Update the event_backward_extremities table now that this
                # event isn't an outlier any more.
                self._update_backward_extremeties(txn, [event])

        return [
            ec for ec in events_and_contexts if ec[0] not in to_remove
        ]

    @classmethod
    def _delete_existing_rows_txn(cls, txn, events_and_contexts):
        if not events_and_contexts:
            # nothing to do here
            return

        logger.info("Deleting existing")

        for table in (
                "events",
                "event_auth",
                "event_json",
                "event_content_hashes",
                "event_destinations",
                "event_edge_hashes",
                "event_edges",
                "event_forward_extremities",
                "event_push_actions",
                "event_reference_hashes",
                "event_search",
                "event_signatures",
                "event_to_state_groups",
                "guest_access",
                "history_visibility",
                "local_invites",
                "room_names",
                "state_events",
                "rejections",
                "redactions",
                "room_memberships",
                "topics"
        ):
            txn.executemany(
                "DELETE FROM %s WHERE event_id = ?" % (table,),
                [(ev.event_id,) for ev, _ in events_and_contexts]
            )

    def _store_event_txn(self, txn, events_and_contexts):
        """Insert new events into the event and event_json tables

        Args:
            txn (twisted.enterprise.adbapi.Connection): db connection
            events_and_contexts (list[(EventBase, EventContext)]): events
                we are persisting
        """

        if not events_and_contexts:
            # nothing to do here
            return

        def event_dict(event):
            d = event.get_dict()
            d.pop("redacted", None)
            d.pop("redacted_because", None)
            return d

        self._simple_insert_many_txn(
            txn,
            table="event_json",
            values=[
                {
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "internal_metadata": encode_json(
                        event.internal_metadata.get_dict()
                    ).decode("UTF-8"),
                    "json": encode_json(event_dict(event)).decode("UTF-8"),
                }
                for event, _ in events_and_contexts
            ],
        )

        self._simple_insert_many_txn(
            txn,
            table="events",
            values=[
                {
                    "stream_ordering": event.internal_metadata.stream_ordering,
                    "topological_ordering": event.depth,
                    "depth": event.depth,
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "type": event.type,
                    "processed": True,
                    "outlier": event.internal_metadata.is_outlier(),
                    "content": encode_json(event.content).decode("UTF-8"),
                    "origin_server_ts": int(event.origin_server_ts),
                    "received_ts": self._clock.time_msec(),
                    "sender": event.sender,
                    "contains_url": (
                        "url" in event.content
                        and isinstance(event.content["url"], basestring)
                    ),
                }
                for event, _ in events_and_contexts
            ],
        )

    def _store_rejected_events_txn(self, txn, events_and_contexts):
        """Add rows to the 'rejections' table for received events which were
        rejected

        Args:
            txn (twisted.enterprise.adbapi.Connection): db connection
            events_and_contexts (list[(EventBase, EventContext)]): events
                we are persisting

        Returns:
            list[(EventBase, EventContext)] new list, without the rejected
                events.
        """
        # Remove the rejected events from the list now that we've added them
        # to the events table and the events_json table.
        to_remove = set()
        for event, context in events_and_contexts:
            if context.rejected:
                # Insert the event_id into the rejections table
                self._store_rejections_txn(
                    txn, event.event_id, context.rejected
                )
                to_remove.add(event)

        return [
            ec for ec in events_and_contexts if ec[0] not in to_remove
        ]

    def _update_metadata_tables_txn(self, txn, events_and_contexts,
                                    all_events_and_contexts, backfilled):
        """Update all the miscellaneous tables for new events

        Args:
            txn (twisted.enterprise.adbapi.Connection): db connection
            events_and_contexts (list[(EventBase, EventContext)]): events
                we are persisting
            all_events_and_contexts (list[(EventBase, EventContext)]): all
                events that we were going to persist. This includes events
                we've already persisted, etc, that wouldn't appear in
                events_and_context.
            backfilled (bool): True if the events were backfilled
        """

        # Insert all the push actions into the event_push_actions table.
        self._set_push_actions_for_event_and_users_txn(
            txn,
            events_and_contexts=events_and_contexts,
            all_events_and_contexts=all_events_and_contexts,
        )

        if not events_and_contexts:
            # nothing to do here
            return

        for event, context in events_and_contexts:
            if event.type == EventTypes.Redaction and event.redacts is not None:
                # Remove the entries in the event_push_actions table for the
                # redacted event.
                self._remove_push_actions_for_event_id_txn(
                    txn, event.room_id, event.redacts
                )

        self._simple_insert_many_txn(
            txn,
            table="event_auth",
            values=[
                {
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "auth_id": auth_id,
                }
                for event, _ in events_and_contexts
                for auth_id, _ in event.auth_events
                if event.is_state()
            ],
        )

        # Update the event_forward_extremities, event_backward_extremities and
        # event_edges tables.
        self._handle_mult_prev_events(
            txn,
            events=[event for event, _ in events_and_contexts],
        )

        for event, _ in events_and_contexts:
            if event.type == EventTypes.Name:
                # Insert into the room_names and event_search tables.
                self._store_room_name_txn(txn, event)
            elif event.type == EventTypes.Topic:
                # Insert into the topics table and event_search table.
                self._store_room_topic_txn(txn, event)
            elif event.type == EventTypes.Message:
                # Insert into the event_search table.
                self._store_room_message_txn(txn, event)
            elif event.type == EventTypes.Redaction:
                # Insert into the redactions table.
                self._store_redaction(txn, event)
            elif event.type == EventTypes.RoomHistoryVisibility:
                # Insert into the event_search table.
                self._store_history_visibility_txn(txn, event)
            elif event.type == EventTypes.GuestAccess:
                # Insert into the event_search table.
                self._store_guest_access_txn(txn, event)

        # Insert into the room_memberships table.
        self._store_room_members_txn(
            txn,
            [
                event
                for event, _ in events_and_contexts
                if event.type == EventTypes.Member
            ],
            backfilled=backfilled,
        )

        # Insert event_reference_hashes table.
        self._store_event_reference_hashes_txn(
            txn, [event for event, _ in events_and_contexts]
        )

        state_events_and_contexts = [
            ec for ec in events_and_contexts if ec[0].is_state()
        ]

        state_values = []
        for event, context in state_events_and_contexts:
            vals = {
                "event_id": event.event_id,
                "room_id": event.room_id,
                "type": event.type,
                "state_key": event.state_key,
            }

            # TODO: How does this work with backfilling?
            if hasattr(event, "replaces_state"):
                vals["prev_state"] = event.replaces_state

            state_values.append(vals)

        self._simple_insert_many_txn(
            txn,
            table="state_events",
            values=state_values,
        )

        self._simple_insert_many_txn(
            txn,
            table="event_edges",
            values=[
                {
                    "event_id": event.event_id,
                    "prev_event_id": prev_id,
                    "room_id": event.room_id,
                    "is_state": True,
                }
                for event, _ in state_events_and_contexts
                for prev_id, _ in event.prev_state
            ],
        )

        # Prefill the event cache
        self._add_to_cache(txn, events_and_contexts)

    def _add_to_cache(self, txn, events_and_contexts):
        to_prefill = []

        rows = []
        N = 200
        for i in range(0, len(events_and_contexts), N):
            ev_map = {
                e[0].event_id: e[0]
                for e in events_and_contexts[i:i + N]
            }
            if not ev_map:
                break

            sql = (
                "SELECT "
                " e.event_id as event_id, "
                " r.redacts as redacts,"
                " rej.event_id as rejects "
                " FROM events as e"
                " LEFT JOIN rejections as rej USING (event_id)"
                " LEFT JOIN redactions as r ON e.event_id = r.redacts"
                " WHERE e.event_id IN (%s)"
            ) % (",".join(["?"] * len(ev_map)),)

            txn.execute(sql, ev_map.keys())
            rows = self.cursor_to_dict(txn)
            for row in rows:
                event = ev_map[row["event_id"]]
                if not row["rejects"] and not row["redacts"]:
                    to_prefill.append(_EventCacheEntry(
                        event=event,
                        redacted_event=None,
                    ))

        def prefill():
            for cache_entry in to_prefill:
                self._get_event_cache.prefill((cache_entry[0].event_id,), cache_entry)
        txn.call_after(prefill)

    def _store_redaction(self, txn, event):
        # invalidate the cache for the redacted event
        txn.call_after(self._invalidate_get_event_cache, event.redacts)
        txn.execute(
            "INSERT INTO redactions (event_id, redacts) VALUES (?,?)",
            (event.event_id, event.redacts)
        )

    @defer.inlineCallbacks
    def have_events_in_timeline(self, event_ids):
        """Given a list of event ids, check if we have already processed and
        stored them as non outliers.
        """
        rows = yield self._simple_select_many_batch(
            table="events",
            retcols=("event_id",),
            column="event_id",
            iterable=list(event_ids),
            keyvalues={"outlier": False},
            desc="have_events_in_timeline",
        )

        defer.returnValue(set(r["event_id"] for r in rows))

    @defer.inlineCallbacks
    def have_seen_events(self, event_ids):
        """Given a list of event ids, check if we have already processed them.

        Args:
            event_ids (iterable[str]):

        Returns:
            Deferred[set[str]]: The events we have already seen.
        """
        results = set()

        def have_seen_events_txn(txn, chunk):
            sql = (
                "SELECT event_id FROM events as e WHERE e.event_id IN (%s)"
                % (",".join("?" * len(chunk)), )
            )
            txn.execute(sql, chunk)
            for (event_id, ) in txn:
                results.add(event_id)

        # break the input up into chunks of 100
        input_iterator = iter(event_ids)
        for chunk in iter(lambda: list(itertools.islice(input_iterator, 100)),
                          []):
            yield self.runInteraction(
                "have_seen_events",
                have_seen_events_txn,
                chunk,
            )
        defer.returnValue(results)

    def get_seen_events_with_rejections(self, event_ids):
        """Given a list of event ids, check if we rejected them.

        Args:
            event_ids (list[str])

        Returns:
            Deferred[dict[str, str|None):
                Has an entry for each event id we already have seen. Maps to
                the rejected reason string if we rejected the event, else maps
                to None.
        """
        if not event_ids:
            return defer.succeed({})

        def f(txn):
            sql = (
                "SELECT e.event_id, reason FROM events as e "
                "LEFT JOIN rejections as r ON e.event_id = r.event_id "
                "WHERE e.event_id = ?"
            )

            res = {}
            for event_id in event_ids:
                txn.execute(sql, (event_id,))
                row = txn.fetchone()
                if row:
                    _, rejected = row
                    res[event_id] = rejected

            return res

        return self.runInteraction("get_rejection_reasons", f)

    @defer.inlineCallbacks
    def count_daily_messages(self):
        """
        Returns an estimate of the number of messages sent in the last day.

        If it has been significantly less or more than one day since the last
        call to this function, it will return None.
        """
        def _count_messages(txn):
            sql = """
                SELECT COALESCE(COUNT(*), 0) FROM events
                WHERE type = 'm.room.message'
                AND stream_ordering > ?
            """
            txn.execute(sql, (self.stream_ordering_day_ago,))
            count, = txn.fetchone()
            return count

        ret = yield self.runInteraction("count_messages", _count_messages)
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def count_daily_sent_messages(self):
        def _count_messages(txn):
            # This is good enough as if you have silly characters in your own
            # hostname then thats your own fault.
            like_clause = "%:" + self.hs.hostname

            sql = """
                SELECT COALESCE(COUNT(*), 0) FROM events
                WHERE type = 'm.room.message'
                    AND sender LIKE ?
                AND stream_ordering > ?
            """

            txn.execute(sql, (like_clause, self.stream_ordering_day_ago,))
            count, = txn.fetchone()
            return count

        ret = yield self.runInteraction("count_daily_sent_messages", _count_messages)
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def count_daily_active_rooms(self):
        def _count(txn):
            sql = """
                SELECT COALESCE(COUNT(DISTINCT room_id), 0) FROM events
                WHERE type = 'm.room.message'
                AND stream_ordering > ?
            """
            txn.execute(sql, (self.stream_ordering_day_ago,))
            count, = txn.fetchone()
            return count

        ret = yield self.runInteraction("count_daily_active_rooms", _count)
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def _background_reindex_fields_sender(self, progress, batch_size):
        target_min_stream_id = progress["target_min_stream_id_inclusive"]
        max_stream_id = progress["max_stream_id_exclusive"]
        rows_inserted = progress.get("rows_inserted", 0)

        INSERT_CLUMP_SIZE = 1000

        def reindex_txn(txn):
            sql = (
                "SELECT stream_ordering, event_id, json FROM events"
                " INNER JOIN event_json USING (event_id)"
                " WHERE ? <= stream_ordering AND stream_ordering < ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]

            update_rows = []
            for row in rows:
                try:
                    event_id = row[1]
                    event_json = json.loads(row[2])
                    sender = event_json["sender"]
                    content = event_json["content"]

                    contains_url = "url" in content
                    if contains_url:
                        contains_url &= isinstance(content["url"], basestring)
                except (KeyError, AttributeError):
                    # If the event is missing a necessary field then
                    # skip over it.
                    continue

                update_rows.append((sender, contains_url, event_id))

            sql = (
                "UPDATE events SET sender = ?, contains_url = ? WHERE event_id = ?"
            )

            for index in range(0, len(update_rows), INSERT_CLUMP_SIZE):
                clump = update_rows[index:index + INSERT_CLUMP_SIZE]
                txn.executemany(sql, clump)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
                "rows_inserted": rows_inserted + len(rows)
            }

            self._background_update_progress_txn(
                txn, self.EVENT_FIELDS_SENDER_URL_UPDATE_NAME, progress
            )

            return len(rows)

        result = yield self.runInteraction(
            self.EVENT_FIELDS_SENDER_URL_UPDATE_NAME, reindex_txn
        )

        if not result:
            yield self._end_background_update(self.EVENT_FIELDS_SENDER_URL_UPDATE_NAME)

        defer.returnValue(result)

    @defer.inlineCallbacks
    def _background_reindex_origin_server_ts(self, progress, batch_size):
        target_min_stream_id = progress["target_min_stream_id_inclusive"]
        max_stream_id = progress["max_stream_id_exclusive"]
        rows_inserted = progress.get("rows_inserted", 0)

        INSERT_CLUMP_SIZE = 1000

        def reindex_search_txn(txn):
            sql = (
                "SELECT stream_ordering, event_id FROM events"
                " WHERE ? <= stream_ordering AND stream_ordering < ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]
            event_ids = [row[1] for row in rows]

            rows_to_update = []

            chunks = [
                event_ids[i:i + 100]
                for i in xrange(0, len(event_ids), 100)
            ]
            for chunk in chunks:
                ev_rows = self._simple_select_many_txn(
                    txn,
                    table="event_json",
                    column="event_id",
                    iterable=chunk,
                    retcols=["event_id", "json"],
                    keyvalues={},
                )

                for row in ev_rows:
                    event_id = row["event_id"]
                    event_json = json.loads(row["json"])
                    try:
                        origin_server_ts = event_json["origin_server_ts"]
                    except (KeyError, AttributeError):
                        # If the event is missing a necessary field then
                        # skip over it.
                        continue

                    rows_to_update.append((origin_server_ts, event_id))

            sql = (
                "UPDATE events SET origin_server_ts = ? WHERE event_id = ?"
            )

            for index in range(0, len(rows_to_update), INSERT_CLUMP_SIZE):
                clump = rows_to_update[index:index + INSERT_CLUMP_SIZE]
                txn.executemany(sql, clump)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
                "rows_inserted": rows_inserted + len(rows_to_update)
            }

            self._background_update_progress_txn(
                txn, self.EVENT_ORIGIN_SERVER_TS_NAME, progress
            )

            return len(rows_to_update)

        result = yield self.runInteraction(
            self.EVENT_ORIGIN_SERVER_TS_NAME, reindex_search_txn
        )

        if not result:
            yield self._end_background_update(self.EVENT_ORIGIN_SERVER_TS_NAME)

        defer.returnValue(result)

    def get_current_backfill_token(self):
        """The current minimum token that backfilled events have reached"""
        return -self._backfill_id_gen.get_current_token()

    def get_current_events_token(self):
        """The current maximum token that events have reached"""
        return self._stream_id_gen.get_current_token()

    def get_all_new_forward_event_rows(self, last_id, current_id, limit):
        if last_id == current_id:
            return defer.succeed([])

        def get_all_new_forward_event_rows(txn):
            sql = (
                "SELECT e.stream_ordering, e.event_id, e.room_id, e.type,"
                " state_key, redacts"
                " FROM events AS e"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events USING (event_id)"
                " WHERE ? < stream_ordering AND stream_ordering <= ?"
                " ORDER BY stream_ordering ASC"
                " LIMIT ?"
            )
            txn.execute(sql, (last_id, current_id, limit))
            new_event_updates = txn.fetchall()

            if len(new_event_updates) == limit:
                upper_bound = new_event_updates[-1][0]
            else:
                upper_bound = current_id

            sql = (
                "SELECT event_stream_ordering, e.event_id, e.room_id, e.type,"
                " state_key, redacts"
                " FROM events AS e"
                " INNER JOIN ex_outlier_stream USING (event_id)"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events USING (event_id)"
                " WHERE ? < event_stream_ordering"
                " AND event_stream_ordering <= ?"
                " ORDER BY event_stream_ordering DESC"
            )
            txn.execute(sql, (last_id, upper_bound))
            new_event_updates.extend(txn)

            return new_event_updates
        return self.runInteraction(
            "get_all_new_forward_event_rows", get_all_new_forward_event_rows
        )

    def get_all_new_backfill_event_rows(self, last_id, current_id, limit):
        if last_id == current_id:
            return defer.succeed([])

        def get_all_new_backfill_event_rows(txn):
            sql = (
                "SELECT -e.stream_ordering, e.event_id, e.room_id, e.type,"
                " state_key, redacts"
                " FROM events AS e"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events USING (event_id)"
                " WHERE ? > stream_ordering AND stream_ordering >= ?"
                " ORDER BY stream_ordering ASC"
                " LIMIT ?"
            )
            txn.execute(sql, (-last_id, -current_id, limit))
            new_event_updates = txn.fetchall()

            if len(new_event_updates) == limit:
                upper_bound = new_event_updates[-1][0]
            else:
                upper_bound = current_id

            sql = (
                "SELECT -event_stream_ordering, e.event_id, e.room_id, e.type,"
                " state_key, redacts"
                " FROM events AS e"
                " INNER JOIN ex_outlier_stream USING (event_id)"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events USING (event_id)"
                " WHERE ? > event_stream_ordering"
                " AND event_stream_ordering >= ?"
                " ORDER BY event_stream_ordering DESC"
            )
            txn.execute(sql, (-last_id, -upper_bound))
            new_event_updates.extend(txn.fetchall())

            return new_event_updates
        return self.runInteraction(
            "get_all_new_backfill_event_rows", get_all_new_backfill_event_rows
        )

    @cached(num_args=5, max_entries=10)
    def get_all_new_events(self, last_backfill_id, last_forward_id,
                           current_backfill_id, current_forward_id, limit):
        """Get all the new events that have arrived at the server either as
        new events or as backfilled events"""
        have_backfill_events = last_backfill_id != current_backfill_id
        have_forward_events = last_forward_id != current_forward_id

        if not have_backfill_events and not have_forward_events:
            return defer.succeed(AllNewEventsResult([], [], [], [], []))

        def get_all_new_events_txn(txn):
            sql = (
                "SELECT e.stream_ordering, e.event_id, e.room_id, e.type,"
                " state_key, redacts"
                " FROM events AS e"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events USING (event_id)"
                " WHERE ? < stream_ordering AND stream_ordering <= ?"
                " ORDER BY stream_ordering ASC"
                " LIMIT ?"
            )
            if have_forward_events:
                txn.execute(sql, (last_forward_id, current_forward_id, limit))
                new_forward_events = txn.fetchall()

                if len(new_forward_events) == limit:
                    upper_bound = new_forward_events[-1][0]
                else:
                    upper_bound = current_forward_id

                sql = (
                    "SELECT event_stream_ordering, event_id, state_group"
                    " FROM ex_outlier_stream"
                    " WHERE ? > event_stream_ordering"
                    " AND event_stream_ordering >= ?"
                    " ORDER BY event_stream_ordering DESC"
                )
                txn.execute(sql, (last_forward_id, upper_bound))
                forward_ex_outliers = txn.fetchall()
            else:
                new_forward_events = []
                forward_ex_outliers = []

            sql = (
                "SELECT -e.stream_ordering, e.event_id, e.room_id, e.type,"
                " state_key, redacts"
                " FROM events AS e"
                " LEFT JOIN redactions USING (event_id)"
                " LEFT JOIN state_events USING (event_id)"
                " WHERE ? > stream_ordering AND stream_ordering >= ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )
            if have_backfill_events:
                txn.execute(sql, (-last_backfill_id, -current_backfill_id, limit))
                new_backfill_events = txn.fetchall()

                if len(new_backfill_events) == limit:
                    upper_bound = new_backfill_events[-1][0]
                else:
                    upper_bound = current_backfill_id

                sql = (
                    "SELECT -event_stream_ordering, event_id, state_group"
                    " FROM ex_outlier_stream"
                    " WHERE ? > event_stream_ordering"
                    " AND event_stream_ordering >= ?"
                    " ORDER BY event_stream_ordering DESC"
                )
                txn.execute(sql, (-last_backfill_id, -upper_bound))
                backward_ex_outliers = txn.fetchall()
            else:
                new_backfill_events = []
                backward_ex_outliers = []

            return AllNewEventsResult(
                new_forward_events, new_backfill_events,
                forward_ex_outliers, backward_ex_outliers,
            )
        return self.runInteraction("get_all_new_events", get_all_new_events_txn)

    def purge_history(
        self, room_id, topological_ordering, delete_local_events,
    ):
        """Deletes room history before a certain point

        Args:
            room_id (str):

            topological_ordering (int):
                minimum topo ordering to preserve

            delete_local_events (bool):
                if True, we will delete local events as well as remote ones
                (instead of just marking them as outliers and deleting their
                state groups).
        """

        return self.runInteraction(
            "purge_history",
            self._purge_history_txn, room_id, topological_ordering,
            delete_local_events,
        )

    def _purge_history_txn(
        self, txn, room_id, topological_ordering, delete_local_events,
    ):
        # Tables that should be pruned:
        #     event_auth
        #     event_backward_extremities
        #     event_content_hashes
        #     event_destinations
        #     event_edge_hashes
        #     event_edges
        #     event_forward_extremities
        #     event_json
        #     event_push_actions
        #     event_reference_hashes
        #     event_search
        #     event_signatures
        #     event_to_state_groups
        #     events
        #     rejections
        #     room_depth
        #     state_groups
        #     state_groups_state

        # we will build a temporary table listing the events so that we don't
        # have to keep shovelling the list back and forth across the
        # connection. Annoyingly the python sqlite driver commits the
        # transaction on CREATE, so let's do this first.
        #
        # furthermore, we might already have the table from a previous (failed)
        # purge attempt, so let's drop the table first.

        txn.execute("DROP TABLE IF EXISTS events_to_purge")

        txn.execute(
            "CREATE TEMPORARY TABLE events_to_purge ("
            "    event_id TEXT NOT NULL,"
            "    should_delete BOOLEAN NOT NULL"
            ")"
        )

        # create an index on should_delete because later we'll be looking for
        # the should_delete / shouldn't_delete subsets
        txn.execute(
            "CREATE INDEX events_to_purge_should_delete"
            " ON events_to_purge(should_delete)",
        )

        # First ensure that we're not about to delete all the forward extremeties
        txn.execute(
            "SELECT e.event_id, e.depth FROM events as e "
            "INNER JOIN event_forward_extremities as f "
            "ON e.event_id = f.event_id "
            "AND e.room_id = f.room_id "
            "WHERE f.room_id = ?",
            (room_id,)
        )
        rows = txn.fetchall()
        max_depth = max(row[0] for row in rows)

        if max_depth <= topological_ordering:
            # We need to ensure we don't delete all the events from the datanase
            # otherwise we wouldn't be able to send any events (due to not
            # having any backwards extremeties)
            raise SynapseError(
                400, "topological_ordering is greater than forward extremeties"
            )

        logger.info("[purge] looking for events to delete")

        should_delete_expr = "state_key IS NULL"
        should_delete_params = ()
        if not delete_local_events:
            should_delete_expr += " AND event_id NOT LIKE ?"
            should_delete_params += ("%:" + self.hs.hostname, )

        should_delete_params += (room_id, topological_ordering)

        txn.execute(
            "INSERT INTO events_to_purge"
            " SELECT event_id, %s"
            " FROM events AS e LEFT JOIN state_events USING (event_id)"
            " WHERE e.room_id = ? AND topological_ordering < ?" % (
                should_delete_expr,
            ),
            should_delete_params,
        )
        txn.execute(
            "SELECT event_id, should_delete FROM events_to_purge"
        )
        event_rows = txn.fetchall()
        logger.info(
            "[purge] found %i events before cutoff, of which %i can be deleted",
            len(event_rows), sum(1 for e in event_rows if e[1]),
        )

        logger.info("[purge] Finding new backward extremities")

        # We calculate the new entries for the backward extremeties by finding
        # all events that point to events that are to be purged
        txn.execute(
            "SELECT DISTINCT e.event_id FROM events_to_purge AS e"
            " INNER JOIN event_edges AS ed ON e.event_id = ed.prev_event_id"
            " INNER JOIN events AS e2 ON e2.event_id = ed.event_id"
            " WHERE e2.topological_ordering >= ?",
            (topological_ordering, )
        )
        new_backwards_extrems = txn.fetchall()

        logger.info("[purge] replacing backward extremities: %r", new_backwards_extrems)

        txn.execute(
            "DELETE FROM event_backward_extremities WHERE room_id = ?",
            (room_id,)
        )

        # Update backward extremeties
        txn.executemany(
            "INSERT INTO event_backward_extremities (room_id, event_id)"
            " VALUES (?, ?)",
            [
                (room_id, event_id) for event_id, in new_backwards_extrems
            ]
        )

        logger.info("[purge] finding redundant state groups")

        # Get all state groups that are only referenced by events that are
        # to be deleted.
        txn.execute(
            "SELECT state_group FROM event_to_state_groups"
            " INNER JOIN events USING (event_id)"
            " WHERE state_group IN ("
            "   SELECT DISTINCT state_group FROM events_to_purge"
            "   INNER JOIN event_to_state_groups USING (event_id)"
            " )"
            " GROUP BY state_group HAVING MAX(topological_ordering) < ?",
            (topological_ordering, )
        )

        state_rows = txn.fetchall()
        logger.info("[purge] found %i redundant state groups", len(state_rows))

        # make a set of the redundant state groups, so that we can look them up
        # efficiently
        state_groups_to_delete = set([sg for sg, in state_rows])

        # Now we get all the state groups that rely on these state groups
        logger.info("[purge] finding state groups which depend on redundant"
                    " state groups")
        remaining_state_groups = []
        for i in xrange(0, len(state_rows), 100):
            chunk = [sg for sg, in state_rows[i:i + 100]]
            # look for state groups whose prev_state_group is one we are about
            # to delete
            rows = self._simple_select_many_txn(
                txn,
                table="state_group_edges",
                column="prev_state_group",
                iterable=chunk,
                retcols=["state_group"],
                keyvalues={},
            )
            remaining_state_groups.extend(
                row["state_group"] for row in rows

                # exclude state groups we are about to delete: no point in
                # updating them
                if row["state_group"] not in state_groups_to_delete
            )

        # Now we turn the state groups that reference to-be-deleted state
        # groups to non delta versions.
        for sg in remaining_state_groups:
            logger.info("[purge] de-delta-ing remaining state group %s", sg)
            curr_state = self._get_state_groups_from_groups_txn(
                txn, [sg], types=None
            )
            curr_state = curr_state[sg]

            self._simple_delete_txn(
                txn,
                table="state_groups_state",
                keyvalues={
                    "state_group": sg,
                }
            )

            self._simple_delete_txn(
                txn,
                table="state_group_edges",
                keyvalues={
                    "state_group": sg,
                }
            )

            self._simple_insert_many_txn(
                txn,
                table="state_groups_state",
                values=[
                    {
                        "state_group": sg,
                        "room_id": room_id,
                        "type": key[0],
                        "state_key": key[1],
                        "event_id": state_id,
                    }
                    for key, state_id in curr_state.iteritems()
                ],
            )

        logger.info("[purge] removing redundant state groups")
        txn.executemany(
            "DELETE FROM state_groups_state WHERE state_group = ?",
            state_rows
        )
        txn.executemany(
            "DELETE FROM state_groups WHERE id = ?",
            state_rows
        )

        logger.info("[purge] removing events from event_to_state_groups")
        txn.execute(
            "DELETE FROM event_to_state_groups "
            "WHERE event_id IN (SELECT event_id from events_to_purge)"
        )
        for event_id, _ in event_rows:
            txn.call_after(self._get_state_group_for_event.invalidate, (
                event_id,
            ))

        # Delete all remote non-state events
        for table in (
            "events",
            "event_json",
            "event_auth",
            "event_content_hashes",
            "event_destinations",
            "event_edge_hashes",
            "event_edges",
            "event_forward_extremities",
            "event_reference_hashes",
            "event_search",
            "event_signatures",
            "rejections",
        ):
            logger.info("[purge] removing events from %s", table)

            txn.execute(
                "DELETE FROM %s WHERE event_id IN ("
                "    SELECT event_id FROM events_to_purge WHERE should_delete"
                ")" % (table,),
            )

        # event_push_actions lacks an index on event_id, and has one on
        # (room_id, event_id) instead.
        for table in (
            "event_push_actions",
        ):
            logger.info("[purge] removing events from %s", table)

            txn.execute(
                "DELETE FROM %s WHERE room_id = ? AND event_id IN ("
                "    SELECT event_id FROM events_to_purge WHERE should_delete"
                ")" % (table,),
                (room_id, )
            )

        # Mark all state and own events as outliers
        logger.info("[purge] marking remaining events as outliers")
        txn.execute(
            "UPDATE events SET outlier = ?"
            " WHERE event_id IN ("
            "    SELECT event_id FROM events_to_purge "
            "    WHERE NOT should_delete"
            ")",
            (True,),
        )

        # synapse tries to take out an exclusive lock on room_depth whenever it
        # persists events (because upsert), and once we run this update, we
        # will block that for the rest of our transaction.
        #
        # So, let's stick it at the end so that we don't block event
        # persistence.
        logger.info("[purge] updating room_depth")
        txn.execute(
            "UPDATE room_depth SET min_depth = ? WHERE room_id = ?",
            (topological_ordering, room_id,)
        )

        # finally, drop the temp table. this will commit the txn in sqlite,
        # so make sure to keep this actually last.
        txn.execute(
            "DROP TABLE events_to_purge"
        )

        logger.info("[purge] done")

    @defer.inlineCallbacks
    def is_event_after(self, event_id1, event_id2):
        """Returns True if event_id1 is after event_id2 in the stream
        """
        to_1, so_1 = yield self._get_event_ordering(event_id1)
        to_2, so_2 = yield self._get_event_ordering(event_id2)
        defer.returnValue((to_1, so_1) > (to_2, so_2))

    @cachedInlineCallbacks(max_entries=5000)
    def _get_event_ordering(self, event_id):
        res = yield self._simple_select_one(
            table="events",
            retcols=["topological_ordering", "stream_ordering"],
            keyvalues={"event_id": event_id},
            allow_none=True
        )

        if not res:
            raise SynapseError(404, "Could not find event %s" % (event_id,))

        defer.returnValue((int(res["topological_ordering"]), int(res["stream_ordering"])))

    def get_max_current_state_delta_stream_id(self):
        return self._stream_id_gen.get_current_token()

    def get_all_updated_current_state_deltas(self, from_token, to_token, limit):
        def get_all_updated_current_state_deltas_txn(txn):
            sql = """
                SELECT stream_id, room_id, type, state_key, event_id
                FROM current_state_delta_stream
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC LIMIT ?
            """
            txn.execute(sql, (from_token, to_token, limit))
            return txn.fetchall()
        return self.runInteraction(
            "get_all_updated_current_state_deltas",
            get_all_updated_current_state_deltas_txn,
        )


AllNewEventsResult = namedtuple("AllNewEventsResult", [
    "new_forward_events", "new_backfill_events",
    "forward_ex_outliers", "backward_ex_outliers",
])
