# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
Tools for scheduling observations.
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import copy
from abc import ABCMeta, abstractmethod

import numpy as np

from astropy import units as u
from astropy.table import Table

from .utils import time_grid_from_range, stride_array
from .constraints import AltitudeConstraint

__all__ = ['ObservingBlock', 'TransitionBlock', 'Schedule', 'Slot', 'Scheduler',
           'SequentialScheduler', 'PriorityScheduler', 'Transitioner', 'Scorer']


class ObservingBlock(object):
    """
    An observation to be scheduled, consisting of a target and associated
    constraints on observations.
    """
    @u.quantity_input(duration=u.second)
    def __init__(self, target, duration, priority, configuration={}, constraints=None):
        """
        Parameters
        ----------
        target: `~astroplan.FixedTarget'
            Target to observe

        duration : `~astropy.units.Quantity`
            exposure time

        priority: integer or float
            priority of this object in the target list. 1 is highest priority,
            no maximum

        configuration : dict
            Configuration metadata

        constraints : list of `~astroplan.constraints.Constraint` objects
            The constraints to apply to this particular observing block.  Note
            that constraints applicable to the entire list should go into the
            scheduler.

        """
        self.target = target
        self.duration = duration
        self.priority = priority
        self.configuration = configuration
        self.constraints = constraints
        self.start_time = self.end_time = None
        self.observer = None

    def __repr__(self):
        orig_repr = object.__repr__(self)
        if self.start_time is None or self.end_time is None:
            return orig_repr.replace('object at',
                                     '({0}, unscheduled) at'
                                     .format(self.target.name))
        else:
            s = '({0}, {1} to {2}) at'.format(self.target.name, self.start_time,
                                              self.end_time)
            return orig_repr.replace('object at', s)

    @property
    def constraints_scores(self):
        if not (self.start_time and self.duration):
            return None
        # TODO: setup a way of caching or defining it as an attribute during scheduling
        elif self.observer:
            return {constraint: constraint(self.observer, [self.target],
                                           times=[self.start_time, self.start_time + self.duration])
                    for constraint in self.constraints}

    @classmethod
    def from_exposures(cls, target, priority, time_per_exposure,
                       number_exposures, readout_time=0 * u.second,
                       configuration={}, constraints=None):
        duration = number_exposures * (time_per_exposure + readout_time)
        ob = cls(target, duration, priority, configuration, constraints)
        ob.time_per_exposure = time_per_exposure
        ob.number_exposures = number_exposures
        ob.readout_time = readout_time
        return ob


class Scorer(object):
    """
    Returns scores and score arrays from the evaluation of constraints on
    observing blocks
    """
    def __init__(self, blocks, observer, schedule, global_constraints=[]):
        """
        Parameters
        ----------
        blocks : list of `~astroplan.scheduling.ObservingBlock` objects
            list of blocks that need to be scored
        observer : `~astroplan.Observer`
            the observer
        schedule : `~astroplan.scheduling.Schedule`
            The schedule inside which the blocks should fit
        global_constraints : list of `~astroplan.Constraint` objects
            any ``Constraint`` that applies to all the blocks
        """
        self.blocks = blocks
        self.observer = observer
        self.schedule = schedule
        self.global_constraints = global_constraints

    def create_score_array(self, time_resolution=1*u.minute):
        """
        this makes a score array over the entire schedule for all of the
        blocks and each `~astroplan.Constraint`.

        Parameters
        ----------
        time_resolution : `~astropy.units.Quantity`
            the time between each scored time
        """
        start = self.schedule.start_time
        end = self.schedule.end_time
        times = time_grid_from_range((start, end), time_resolution)
        score_array = np.ones((len(self.blocks), len(times)))
        for i, block in enumerate(self.blocks):
            # TODO: change the default constraints from None to []
            if block.constraints:
                for constraint in block.constraints:
                    applied_score = constraint(self.observer, [block.target],
                                               times=times)[0]
                    score_array[i] *= applied_score
        targets = [block.target for block in self.blocks]
        for constraint in self.global_constraints:
            score_array *= constraint(self.observer, targets, times)
        return score_array

    @classmethod
    def from_start_end(cls, blocks, observer, start_time, end_time,
                       global_constraints=[]):
        """
        for if you don't have a schedule/ aren't inside a scheduler
        """
        dummy_schedule = Schedule(start_time, end_time)
        sc = cls(blocks, observer, dummy_schedule, global_constraints)
        return sc


class TransitionBlock(object):
    """
    Parameterizes the "dead time", e.g. between observations, while the
    telescope is slewing, instrument is reconfiguring, etc.
    """
    def __init__(self, components, start_time=None):
        """
        Parameters
        ----------
        components : dict
            A dictionary mapping the reason for an observation's dead time to
            `~astropy.units.Quantity` objects with time units

        start_time : `~astropy.units.Quantity`
            Start time of observation
        """
        self._components = None
        self.duration = None
        self.start_time = start_time
        self.components = components

    def __repr__(self):
        orig_repr = object.__repr__(self)
        comp_info = ', '.join(['{0}: {1}'.format(c, t)
                               for c, t in self.components.items()])
        if self.start_time is None or self.end_time is None:
            return orig_repr.replace('object at', ' ({0}, unscheduled) at'.format(comp_info))
        else:
            s = '({0}, {1} to {2}) at'.format(comp_info, self.start_time, self.end_time)
            return orig_repr.replace('object at', s)

    @property
    def end_time(self):
        return self.start_time + self.duration

    @property
    def components(self):
        return self._components

    @components.setter
    def components(self, val):
        duration = 0*u.second
        for t in val.values():
            duration += t

        self._components = val
        self.duration = duration

    @classmethod
    @u.quantity_input(duration=u.second)
    def from_duration(cls, duration):
        # for testing how to put transitions between observations during
        # scheduling without considering the complexities of duration
        tb = TransitionBlock({'duration': duration})
        return tb


class Schedule(object):
    """
    An object that represents a schedule, consisting ofa list of
    `~astroplan.scheduling.Slot` objects
    """
    # as currently written, there should be no consecutive unoccupied slots
    # this should change to allow for more flexibility (e.g. dark slots, grey slots)

    def __init__(self, start_time, end_time, constraints=None):
        """
        Parameters:
        -----------
        start_time : `~astropy.time.Time`
            The starting time of the schedule; the start of your
            observing window
        end_time : `~astropy.time.Time`
           The ending time of the schedule; the end of your
           observing window
        constraints : sequence of `Constraint`s
           these are constraints that apply to the entire schedule
        """
        self.start_time = start_time
        self.end_time = end_time
        self.slots = [Slot(start_time, end_time)]
        self.observer = None

    def __repr__(self):
        return ('Schedule containing ' + str(len(self.observing_blocks)) +
                ' observing blocks between ' + str(self.slots[0].start.iso) +
                ' and ' + str(self.slots[-1].end.iso))

    @property
    def observing_blocks(self):
        return [slot.block for slot in self.slots if isinstance(slot.block, ObservingBlock)]

    @property
    def scheduled_blocks(self):
        return [slot.block for slot in self.slots if slot.block]

    @property
    def open_slots(self):
        return [slot for slot in self.slots if not slot.occupied]

    def to_table(self, show_transitions=True, show_unused=False):
        # TODO: allow different coordinate types
        target_names = []
        start_times = []
        end_times = []
        durations = []
        ra = []
        dec = []
        for slot in self.slots:
            if hasattr(slot.block, 'target'):
                start_times.append(slot.start.iso)
                end_times.append(slot.end.iso)
                durations.append(slot.duration.to(u.minute).value)
                target_names.append(slot.block.target.name)
                ra.append(slot.block.target.ra)
                dec.append(slot.block.target.dec)
            elif show_transitions and slot.block:
                start_times.append(slot.start.iso)
                end_times.append(slot.end.iso)
                durations.append(slot.duration.to(u.minute).value)
                target_names.append('TransitionBlock')
                ra.append('')
                dec.append('')
            elif slot.block is None and show_unused:
                start_times.append(slot.start.iso)
                end_times.append(slot.end.iso)
                durations.append(slot.duration.to(u.minute).value)
                target_names.append('Unused Time')
                ra.append('')
                dec.append('')
        return Table([target_names, start_times, end_times, durations, ra, dec],
                     names=('target', 'start time (UTC)', 'end time (UTC)',
                            'duration (minutes)', 'ra', 'dec'))

    def new_slots(self, slot_index, start_time, end_time):
        # this is intended to be used such that there aren't consecutive unoccupied slots
        new_slots = self.slots[slot_index].split_slot(start_time, end_time)
        return new_slots

    def insert_slot(self, start_time, block):
        # due to float representation, this will change block start time
        # and duration by up to 1 second in order to fit in a slot
        for j, slot in enumerate(self.slots):
            if ((slot.start < start_time or abs(slot.start-start_time) < 1*u.second)
                    and (slot.end > start_time + 1*u.second)):
                slot_index = j
        if (block.duration - self.slots[slot_index].duration) > 1*u.second:
            print(self.slots[slot_index].duration.to(u.second), block.duration)
            raise ValueError('longer block than slot')
        elif self.slots[slot_index].end - block.duration < start_time:
            start_time = self.slots[slot_index].end - block.duration

        if abs((self.slots[slot_index].duration - block.duration) < 1 * u.second):
            block.duration = self.slots[slot_index].duration
            start_time = self.slots[slot_index].start
            end_time = self.slots[slot_index].end
        elif abs(self.slots[slot_index].start - start_time) < 1*u.second:
            start_time = self.slots[slot_index].start
            end_time = start_time + block.duration
        elif abs(self.slots[slot_index].end - start_time - block.duration) < 1*u.second:
            end_time = self.slots[slot_index].end
        else:
            end_time = start_time + block.duration
        if isinstance(block, ObservingBlock):
            # TODO: make it shift observing/transition blocks to fill small amounts of open space
            block.end_time = start_time+block.duration
        earlier_slots = self.slots[:slot_index]
        later_slots = self.slots[slot_index+1:]
        block.start_time = start_time
        new_slots = self.new_slots(slot_index, start_time, end_time)
        for new_slot in new_slots:
            if new_slot.middle:
                new_slot.occupied = True
                new_slot.block = block
        self.slots = earlier_slots + new_slots + later_slots
        return earlier_slots + new_slots + later_slots

    def change_slot_block(self, slot_index, new_block=None):
        # currently only written to work for TransitionBlocks in PriorityScheduler
        # made with the assumption that the slot afterwards is open and that the
        # start time will remain the same
        new_end = self.slots[slot_index].start + new_block.duration
        self.slots[slot_index].end = new_end
        self.slots[slot_index].block = new_block
        if self.slots[slot_index + 1].block:
            raise IndexError('slot afterwards is full')
        self.slots[slot_index + 1].start = new_end


class Slot(object):
    """
    A time slot consisting of a start and end time
    """

    def __init__(self, start_time, end_time):
        """
        Parameters:
        -----------
        start_time : `~astropy.time.Time`
            The starting time of the slot
        end_time : `~astropy.time.Time`
            The ending time of the slot
        """
        self.start = start_time
        self.end = end_time
        self.occupied = False
        self.middle = False
        self.block = None

    @property
    def duration(self):
        return self.end - self.start

    def split_slot(self, early_time, later_time):
        # check if the new slot would overwrite occupied/other slots
        if self.occupied:
            raise ValueError('slot is already occupied')

        new_slot = Slot(early_time, later_time)
        new_slot.middle = True
        early_slot = Slot(self.start, early_time)
        late_slot = Slot(later_time, self.end)

        if early_time > self.start and later_time < self.end:
            return [early_slot, new_slot, late_slot]
        elif early_time > self.start:
            return [early_slot, new_slot]
        elif later_time < self.end:
            return [new_slot, late_slot]
        else:
            return [new_slot]


class Scheduler(object):
    """
    Schedule a set of `~astroplan.scheduling.ObservingBlock` objects
    """

    __metaclass__ = ABCMeta

    @u.quantity_input(gap_time=u.second, time_resolution=u.second)
    def __init__(self, constraints, observer, transitioner=None,
                 gap_time=5*u.min, time_resolution=20*u.second):
        """
        Parameters
        ----------
        constraints : sequence of `~astroplan.constraints.Constraint`
            The constraints to apply to *every* observing block.  Note that
            constraints for specific blocks can go on each block individually.
        observer : `~astroplan.Observer`
            The observer/site to do the scheduling for.
        transitioner : `~astroplan.scheduling.Transitioner` or None
            The object to use for computing transition times between blocks.
        gap_time : `~astropy.units.Quantity` with time units
            The maximum length of time a transition between ObservingBlocks
            could take.
        time_resolution : `~astropy.units.Quantity` with time units
            The smallest factor of time used in scheduling, all Blocks scheduled
            will have a duration that is a multiple of it.
        """
        self.constraints = constraints
        self.observer = observer
        self.transitioner = transitioner
        self.gap_time = gap_time
        self.time_resolution = time_resolution

    def __call__(self, blocks, schedule):
        """
        Schedule a set of `~astroplan.scheduling.ObservingBlock` objects.

        Parameters
        ----------
        blocks : list of `~astroplan.scheduling.ObservingBlock` objects
            The observing blocks to schedule.  Note that the input
            `~astroplan.scheduling.ObservingBlock` objects will *not* be
            modified - new ones will be created and returned.
        schedule : `~astroplan.scheduling.Schedule` object
            A schedule that the blocks will be scheduled in. At this time
            the ``schedule`` must be empty, only defined by a start and
            end time.

        Returns
        -------
        schedule : `~astroplan.scheduling.Schedule`
            A schedule objects which consists of `~astroplan.scheduling.Slot`
            objects with and without populated ``block`` objects containing either
            `~astroplan.scheduling.TransitionBlock` or `~astroplan.scheduling.ObservingBlock`
            objects with populated ``start_time`` and ``end_time`` or ``duration`` attributes
        """
        self.schedule = schedule
        # these are *shallow* copies
        copied_blocks = [copy.copy(block) for block in blocks]
        schedule = self._make_schedule(copied_blocks)
        return schedule

    @abstractmethod
    def _make_schedule(self, blocks):
        """
        Does the actual business of scheduling. The ``blocks`` passed in should
        have their ``start_time` and `end_time`` modified to reflect the
        schedule. Any necessary `~astroplan.scheduling.TransitionBlock` should
        also be added.  Then the full set of blocks should be returned as a list
        of blocks, along with a boolean indicating whether or not they have been
        put in order already.

        Parameters
        ----------
        blocks : list of `~astroplan.scheduling.ObservingBlock` objects
            Can be modified as it is already copied by ``__call__``
         Returns
        -------
        schedule : `~astroplan.scheduling.Schedule`
            A schedule objects which consists of `~astroplan.scheduling.Slot`
            objects with and without populated ``block`` objects containing either
            `~astroplan.scheduling.TransitionBlock` or `~astroplan.scheduling.ObservingBlock`
            objects with populated ``start_time`` and ``end_time`` or ``duration`` attributes.
        """
        raise NotImplementedError
        return schedule

    @classmethod
    @u.quantity_input(duration=u.second)
    def from_timespan(cls, center_time, duration, **kwargs):
        """
        Create a new instance of this class given a center time and duration.

        Parameters
        ----------
        center_time : `~astropy.time.Time`
            Mid-point of time-span to schedule.

        duration : `~astropy.units.Quantity` or `~astropy.time.TimeDelta`
            Duration of time-span to schedule
        """
        start_time = center_time - duration / 2.
        end_time = center_time + duration / 2.
        return cls(start_time, end_time, **kwargs)


class SequentialScheduler(Scheduler):
    """
    A scheduler that does "stupid simple sequential scheduling".  That is, it
    simply looks at all the blocks, picks the best one, schedules it, and then
    moves on.
    """
    def __init__(self, *args, **kwargs):
        super(SequentialScheduler, self).__init__(*args, **kwargs)

    def _make_schedule(self, blocks):
        for b in blocks:
            if b.constraints is None:
                b._all_constraints = self.constraints
            else:
                b._all_constraints = self.constraints + b.constraints
            # to make sure the scheduler has some constraint to work off of
            # and to prevent scheduling of targets below the horizon
            if b._all_constraints is None:
                b._all_constraints = [AltitudeConstraint(min=0*u.deg)]
            elif not any(isinstance(c, AltitudeConstraint) for c in b._all_constraints):
                b._all_constraints.append(AltitudeConstraint(min=0*u.deg))
            b._duration_offsets = u.Quantity([0*u.second, b.duration/2,
                                              b.duration])
            b.observer = self.observer
        current_time = self.schedule.start_time
        while (len(blocks) > 0) and (current_time < self.schedule.end_time):
            # first compute the value of all the constraints for each block
            # given the current starting time
            block_transitions = []
            block_constraint_results = []
            for b in blocks:
                # first figure out the transition
                if len(self.schedule.observing_blocks) > 0:
                    trans = self.transitioner(self.schedule.observing_blocks[-1], b, current_time,
                                              self.observer)
                else:
                    trans = None
                block_transitions.append(trans)
                transition_time = 0*u.second if trans is None else trans.duration

                times = current_time + transition_time + b._duration_offsets

                constraint_res = []
                for constraint in b._all_constraints:
                    constraint_res.append(constraint(self.observer, [b.target],
                                                     times))
                # take the product over all the constraints *and* times
                block_constraint_results.append(np.prod(constraint_res))

            # now identify the block that's the best
            bestblock_idx = np.argmax(block_constraint_results)

            if block_constraint_results[bestblock_idx] == 0.:
                # if even the best is unobservable, we need a gap
                current_time += self.gap_time
            else:
                # If there's a best one that's observable, first get its transition
                trans = block_transitions.pop(bestblock_idx)
                if trans is not None:
                    self.schedule.insert_slot(trans.start_time, trans)
                    current_time += trans.duration

                # now assign the block itself times and add it to the schedule
                newb = blocks.pop(bestblock_idx)
                newb.start_time = current_time
                current_time += newb.duration
                newb.end_time = current_time
                newb.constraints_value = block_constraint_results[bestblock_idx]

                self.schedule.insert_slot(newb.start_time, newb)

        return self.schedule


class PriorityScheduler(Scheduler):
    """
    A scheduler that optimizes a prioritized list.  That is, it
    finds the best time for each ObservingBlock, in order of priority.
    """

    def __init__(self, *args, **kwargs):
        """

        """
        super(PriorityScheduler, self).__init__(*args, **kwargs)

    def _make_schedule(self, blocks):
        # Combine individual constraints with global constraints, and
        # retrieve priorities from each block to define scheduling order

        _all_times = []
        _block_priorities = np.zeros(len(blocks))
        for i, b in enumerate(blocks):
            if b.constraints is None:
                b._all_constraints = self.constraints
            else:
                b._all_constraints = self.constraints + b.constraints
            # to make sure the scheduler has some constraint to work off of
            # and to prevent scheduling of targets below the horizon
            if b._all_constraints is None:
                b._all_constraints = [AltitudeConstraint(min=0*u.deg)]
            elif not any(isinstance(c, AltitudeConstraint) for c in b._all_constraints):
                b._all_constraints.append(AltitudeConstraint(min=0*u.deg))
            b._duration_offsets = u.Quantity([0 * u.second, b.duration / 2, b.duration])
            _block_priorities[i] = b.priority
            _all_times.append(b.duration)
            b.observer = self.observer

        # Define a master schedule
        # Generate grid of time slots, and a mask for previous observations

        time_resolution = self.time_resolution
        times = time_grid_from_range([self.schedule.start_time, self.schedule.end_time],
                                     time_resolution=time_resolution)
        is_open_time = np.ones(len(times), bool)

        # generate the score arrays for all of the blocks
        scorer = Scorer(blocks, self.observer, self.schedule, global_constraints=self.constraints)
        score_array = scorer.create_score_array(time_resolution)

        # Sort the list of blocks by priority
        sorted_indices = np.argsort(_block_priorities)

        unscheduled_blocks = []
        # Compute the optimal observation time in priority order
        for i in sorted_indices:
            b = blocks[i]
            # Compute possible observing times by combining object constraints
            # with the master open times mask
            constraint_scores = score_array[i]

            # Add up the applied constraints to prioritize the best blocks
            # And then remove any times that are already scheduled
            constraint_scores[is_open_time == False] = 0
            # Select the most optimal time

            # need to leave time around the Block for transitions
            if self.transitioner.instrument_reconfig_times:
                max_config_time = sum([max(value.values()) for value in
                                       self.transitioner.instrument_reconfig_times.values()])
            else:
                max_config_time = 0*u.second
            if self.transitioner.slew_rate:
                buffer_time = (160*u.deg/self.transitioner.slew_rate + max_config_time)
            else:
                buffer_time = max_config_time
            # TODO: make it so that this isn't required to prevent errors in slot creation
            total_duration = b.duration + buffer_time
            # calculate the number of time slots needed for this exposure
            _stride_by = np.int(np.ceil(float(total_duration / time_resolution)))

            # Stride the score arrays by that number
            _strided_scores = stride_array(constraint_scores, _stride_by)

            # Collapse the sub-arrays
            # (run them through scorekeeper again? Just add them?
            # If there's a zero anywhere in there, def. have to skip)
            good = np.all(_strided_scores > 1e-5, axis=1)
            sum_scores = np.zeros(len(_strided_scores))
            sum_scores[good] = np.sum(_strided_scores[good], axis=1)

            if np.all(constraint_scores == 0) or np.all(good == False):
                # No further calculation if no times meet the constraints
                _is_scheduled = False

            else:
                # If an optimal block is available, _is_scheduled=True
                best_time_idx = np.argmax(sum_scores)
                start_time_idx = best_time_idx
                new_start_time = times[best_time_idx]
                _is_scheduled = True

            if _is_scheduled:
                # set duration such that the Block will fit in the strided array
                duration_indices = np.int(np.ceil(float(b.duration / time_resolution)))
                b.duration = duration_indices * time_resolution
                # add 1 second to the start time to allow for scheduling at the start of a slot
                slot_index = [q for q, slot in enumerate(self.schedule.slots)
                              if slot.start < new_start_time + 1*u.second < slot.end][0]
                slots_before = self.schedule.slots[:slot_index]
                slots_after = self.schedule.slots[slot_index + 1:]
                # this has to remake transitions between already existing ObservingBlocks
                if slots_before:
                    if isinstance(self.schedule.slots[slot_index - 1].block, ObservingBlock):
                        # make a transition object after the previous ObservingBlock
                        tb = self.transitioner(self.schedule.slots[slot_index - 1].block, b,
                                               self.schedule.slots[slot_index - 1].end, self.observer)
                        times_indices = np.int(np.ceil(float(tb.duration / time_resolution)))
                        tb.duration = times_indices * time_resolution
                        start_idx = self.schedule.slots[slot_index - 1].block.end_idx
                        end_idx = times_indices + start_idx
                        # this may make some OBs get sub-optimal scheduling, but it closes gaps
                        # TODO: determine a reasonable range inside which it gets shifted
                        if (new_start_time - tb.start_time < tb.duration or
                                abs(new_start_time - tb.end_time) < self.gap_time):
                            new_start_time = tb.end_time
                            start_time_idx = end_idx
                        self.schedule.insert_slot(tb.start_time, tb)
                        is_open_time[start_idx: end_idx] = False
                        slot_index += 1
                        # Remove times from the master time list (copied in later code blocks)
                    elif isinstance(self.schedule.slots[slot_index - 1].block, TransitionBlock):
                        # change the existing TransitionBlock to what it needs to be now
                        tb = self.transitioner(self.schedule.slots[slot_index - 2].block, b,
                                               self.schedule.slots[slot_index - 2].end, self.observer)
                        times_indices = np.int(np.ceil(float(tb.duration / time_resolution)))
                        tb.duration = times_indices * time_resolution
                        start_idx = self.schedule.slots[slot_index - 2].block.end_idx
                        end_idx = times_indices + start_idx
                        self.schedule.change_slot_block(slot_index - 1, new_block=tb)
                        if (new_start_time - tb.start_time < tb.duration or
                                abs(new_start_time - tb.end_time) < self.gap_time):
                            new_start_time = tb.end_time
                            start_time_idx = end_idx
                        is_open_time[start_idx: end_idx] = False
                end_time_idx = duration_indices + start_time_idx

                if slots_after:
                    if isinstance(self.schedule.slots[slot_index + 1].block, ObservingBlock):
                        # make a transition object after the new ObservingBlock
                        tb = self.transitioner(b, self.schedule.slots[slot_index + 1].block,
                                               new_start_time + b.duration, self.observer)
                        times_indices = np.int(np.ceil(float(tb.duration / time_resolution)))
                        tb.duration = times_indices * time_resolution
                        self.schedule.insert_slot(tb.start_time, tb)
                        start_idx = end_time_idx
                        end_idx = start_idx + times_indices
                        is_open_time[start_idx: end_idx] = False

                # now assign the block itself times and add it to the schedule
                b.constraints = b._all_constraints
                b.end_idx = end_time_idx
                self.schedule.insert_slot(new_start_time, b)
                is_open_time[start_time_idx: end_time_idx] = False

            else:
                print("could not schedule", b.target.name)
                unscheduled_blocks.append(b)
                continue

        return self.schedule


class Transitioner(object):
    """
    A class that defines how to compute transition times from one block to
    another.
    """
    u.quantity_input(slew_rate=u.deg/u.second)

    def __init__(self, slew_rate=None, instrument_reconfig_times=None):
        """
        Parameters
        ----------
        slew_rate : `~astropy.units.Quantity` with angle/time units
            The slew rate of the telescope
        instrument_reconfig_times : dict of dicts or None
            If not None, gives a mapping from property names to another
            dictionary. The second dictionary maps 2-tuples of states to the
            time it takes to transition between those states (as an
            `~astropy.units.Quantity`), can also take a 'default' key
            mapped to a default transition time.
        """
        self.slew_rate = slew_rate
        self.instrument_reconfig_times = instrument_reconfig_times

    def __call__(self, oldblock, newblock, start_time, observer):
        """
        Determines the amount of time needed to transition from one observing
        block to another.  This uses the parameters defined in
        ``self.instrument_reconfig_times``.

        Parameters
        ----------
        oldblock : `~astroplan.scheduling.ObservingBlock` or None
            The initial configuration/target
        newblock : `~astroplan.scheduling.ObservingBlock` or None
            The new configuration/target to transition to
        start_time : `~astropy.time.Time`
            The time the transition should start
        observer : `astroplan.Observer`
            The observer at the time

        Returns
        -------
        transition : `~astroplan.scheduling.TransitionBlock` or None
            A transition to get from ``oldblock`` to ``newblock`` or `None` if
            no transition is necessary
        """
        components = {}
        if self.slew_rate is not None:
            # use the constraints cache for now, but should move that machinery
            # to observer
            from .constraints import _get_altaz
            from astropy.time import Time

            aaz = _get_altaz(Time([start_time]), observer,
                             [oldblock.target, newblock.target])['altaz']
            # TODO: make this [0] unnecessary by fixing _get_altaz to behave well in scalar-time case
            sep = aaz[0].separation(aaz[1])[0]

            components['slew_time'] = sep / self.slew_rate
        if self.instrument_reconfig_times is not None:
            components.update(self.compute_instrument_transitions(oldblock, newblock))

        if components:
            return TransitionBlock(components, start_time)
        else:
            return TransitionBlock.from_duration(0*u.second)

    def compute_instrument_transitions(self, oldblock, newblock):
        components = {}
        for conf_name, old_conf in oldblock.configuration.items():
            if conf_name in newblock.configuration:
                conf_times = self.instrument_reconfig_times.get(conf_name,
                                                                None)
                if conf_times is not None:
                    new_conf = newblock.configuration[conf_name]
                    ctime = conf_times.get((old_conf, new_conf), None)
                    def_time = conf_times.get('default', None)
                    if ctime is not None:
                        s = '{0}:{1} to {2}'.format(conf_name, old_conf,
                                                    new_conf)
                        components[s] = ctime
                    elif def_time and not old_conf == new_conf:
                        s = '{0}:{1} to {2}'.format(conf_name, old_conf,
                                                    new_conf)
                        components[s] = def_time

        return components
