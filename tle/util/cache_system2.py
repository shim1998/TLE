import asyncio
import logging
import time

from discord.ext import commands

from tle.util import codeforces_common as cf_common
from tle.util import codeforces_api as cf
from tle.util import events
from tle.util import tasks
from tle.util.ranklist import Ranklist

logger = logging.getLogger(__name__)


class CacheError(commands.CommandError):
    pass


class ContestCacheError(CacheError):
    pass


class ContestNotFound(ContestCacheError):
    def __init__(self, contest_id):
        super().__init__(f'Contest with id `{contest_id}` not found')
        self.contest_id = contest_id


class ContestCache:
    _NORMAL_CONTEST_RELOAD_DELAY = 30 * 60
    _EXCEPTION_CONTEST_RELOAD_DELAY = 5 * 60
    _ACTIVE_CONTEST_RELOAD_DELAY = 5 * 60
    _ACTIVATE_BEFORE = 20 * 60

    _RUNNING_PHASES = ('CODING', 'PENDING_SYSTEM_TEST', 'SYSTEM_TEST')

    def __init__(self, cache_master):
        self.cache_master = cache_master

        self.contests = []
        self.contest_by_id = {}
        self.contests_by_phase = {phase: [] for phase in cf.Contest.PHASES}
        self.contests_by_phase['_RUNNING'] = []
        self.contests_last_cache = 0

        self.reload_lock = asyncio.Lock()
        self.reload_exception = None
        self.next_delay = None

        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self):
        await self._try_disk()
        self._update_task.start()

    async def reload_now(self):
        """Force a reload. If currently reloading it will wait until done."""
        reloading = self.reload_lock.locked()
        if reloading:
            # Wait until reload complete.
            # To wait until lock is free, await acquire then release immediately.
            async with self.reload_lock:
                pass
        else:
            await self._update_task.manual_trigger()

        if self.reload_exception:
            raise self.reload_exception

    def get_contest(self, contest_id):
        try:
            return self.contest_by_id[contest_id]
        except KeyError:
            raise ContestNotFound(contest_id)

    def get_problemset(self, contest_id):
        return self.cache_master.conn.get_problemset_from_contest(contest_id)

    def get_contests_in_phase(self, phase):
        return self.contests_by_phase[phase]

    async def _try_disk(self):
        async with self.reload_lock:
            contests = self.cache_master.conn.fetch_contests()
            if not contests:
                self.logger.info('Contest cache on disk is empty.')
                return
            await self._update(contests, from_api=False)

    @tasks.task_spec(name='ContestCacheUpdate')
    async def _update_task(self, _):
        async with self.reload_lock:
            self.next_delay = await self._reload_contests()
        self.reload_exception = None

    @_update_task.waiter()
    async def _update_task_waiter(self):
        await asyncio.sleep(self.next_delay)

    @_update_task.exception_handler()
    async def _update_task_exception_handler(self, ex):
        self.reload_exception = ex
        self.next_delay = self._EXCEPTION_CONTEST_RELOAD_DELAY

    async def _reload_contests(self):
        contests = await cf.contest.list()
        delay = await self._update(contests)
        return delay

    async def _update(self, contests, from_api=True):
        self.logger.info(f'{len(contests)} contests fetched from {"API" if from_api else "disk"}')
        contests.sort(key=lambda contest: (contest.startTimeSeconds, contest.id))

        if from_api:
            rc = self.cache_master.conn.cache_contests(contests)
            self.logger.info(f'{rc} contests stored in database')

        contests_by_phase = {phase: [] for phase in cf.Contest.PHASES}
        contests_by_phase['_RUNNING'] = []
        contest_by_id = {}
        for contest in contests:
            contests_by_phase[contest.phase].append(contest)
            contest_by_id[contest.id] = contest
            if contest.phase in self._RUNNING_PHASES:
                contests_by_phase['_RUNNING'].append(contest)

        now = time.time()
        delay = self._NORMAL_CONTEST_RELOAD_DELAY

        for contest in contests_by_phase['BEFORE']:
            at = contest.startTimeSeconds - self._ACTIVATE_BEFORE
            if at > now:
                # Reload at _ACTIVATE_BEFORE before contest to monitor contest delays.
                delay = min(delay, at - now)
            else:
                # The contest starts in <= _ACTIVATE_BEFORE.
                # Reload at contest start, or after _ACTIVE_CONTEST_RELOAD_DELAY, whichever comes first.
                delay = min(contest.startTimeSeconds - now, self._ACTIVE_CONTEST_RELOAD_DELAY)

        if contests_by_phase['_RUNNING']:
            # If any contest is running, reload at an increased rate to detect FINISHED
            delay = min(delay, self._ACTIVE_CONTEST_RELOAD_DELAY)

        self.contests = contests
        self.contests_by_phase = contests_by_phase
        self.contest_by_id = contest_by_id
        self.contests_last_cache = time.time()

        cf_common.event_sys.dispatch(events.ContestListRefresh, self.contests.copy())

        return delay


class ProblemCache:
    _RELOAD_INTERVAL = 6 * 60 * 60

    def __init__(self, cache_master):
        self.cache_master = cache_master

        self.problems = []
        self.problem_by_name = {}
        self.problems_last_cache = 0

        self.reload_lock = asyncio.Lock()
        self.reload_exception = None

        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self):
        await self._try_disk()
        self._update_task.start()

    async def reload_now(self):
        """Force a reload. If currently reloading it will wait until done."""
        reloading = self.reload_lock.locked()
        if reloading:
            # Wait until reload complete.
            # To wait until lock is free, await acquire then release immediately.
            async with self.reload_lock:
                pass
        else:
            await self._update_task.manual_trigger()

        if self.reload_exception:
            raise self.reload_exception

    async def _try_disk(self):
        async with self.reload_lock:
            problems = self.cache_master.conn.fetch_problems()
            if not problems:
                self.logger.info('Problem cache on disk is empty.')
                return
            self.problems = problems
            self.problem_by_name = {problem.name: problem for problem in problems}
            self.logger.info(f'{len(self.problems)} problems fetched from disk')

    @tasks.task_spec(name='ProblemCacheUpdate',
                     waiter=tasks.Waiter.fixed_delay(_RELOAD_INTERVAL))
    async def _update_task(self, _):
        async with self.reload_lock:
            await self._reload_problems()
        self.reload_exception = None

    @_update_task.exception_handler()
    async def _update_task_exception_handler(self, ex):
        self.reload_exception = ex

    async def _reload_problems(self):
        problems, _ = await cf.problemset.problems()
        await self._update(problems)

    async def _update(self, problems):
        self.logger.info(f'{len(problems)} problems fetched from API')
        contest_map = {problem.contestId: self.cache_master.contest_cache.contest_by_id.get(problem.contestId)
                       for problem in problems}

        def keep(problem):
            return (contest_map[problem.contestId] and
                    problem.has_metadata())

        filtered_problems = list(filter(keep, problems))
        problem_by_name = {
            problem.name: problem  # This will discard some valid problems
            for problem in filtered_problems
        }
        self.logger.info(f'Keeping {len(problem_by_name)} problems')

        self.problems = list(problem_by_name.values())
        self.problem_by_name = problem_by_name
        self.problems_last_cache = time.time()

        rc = self.cache_master.conn.cache_problems(self.problems)
        self.logger.info(f'{rc} problems stored in database')


class ProblemsetCacheError(CacheError):
    pass


class ProblemsetNotCached(ProblemsetCacheError):
    def __init__(self, contest_id):
        super().__init__(f'Problemset for contest with id {contest_id} not cached.')


class ProblemsetCache:
    _MONITOR_PERIOD_SINCE_CONTEST_END = 14 * 24 * 60 * 60
    _RELOAD_DELAY = 60 * 60

    def __init__(self, cache_master):
        self.cache_master = cache_master
        self.update_lock = asyncio.Lock()
        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self):
        if self.cache_master.conn.problemset_empty():
            self.logger.warning('Problemset cache on disk is empty. This must be populated '
                                'manually before use.')
        self._update_task.start()

    async def update_for_contest(self, contest_id):
        """Update problemset for a particular contest. Intended for manual trigger."""
        async with self.update_lock:
            contest = self.cache_master.contest_cache.get_contest(contest_id)
            problemset, _ = await self._fetch_problemsets([contest], force_fetch=True)
            self.cache_master.conn.clear_problemset(contest_id)
            self._save_problems(problemset)
            return len(problemset)

    async def update_for_all(self):
        """Update problemsets for all finished contests. Intended for manual trigger."""
        async with self.update_lock:
            contests = self.cache_master.contest_cache.contests_by_phase['FINISHED']
            problemsets, _ = await self._fetch_problemsets(contests, force_fetch=True)
            self.cache_master.conn.clear_problemset()
            self._save_problems(problemsets)
            return len(problemsets)

    @tasks.task_spec(name='ProblemsetCacheUpdate',
                     waiter=tasks.Waiter.fixed_delay(_RELOAD_DELAY))
    async def _update_task(self, _):
        async with self.update_lock:
            contests = self.cache_master.contest_cache.contests_by_phase['FINISHED']
            new_problems, updated_problems = await self._fetch_problemsets(contests)
            self._save_problems(new_problems + updated_problems)
            self.logger.info(f'{len(new_problems)} new problems saved and {len(updated_problems)} '
                             'saved problems updated.')

    async def _fetch_problemsets(self, contests, *, force_fetch=False):
        # We assume it is possible for problems in the same contest to get assigned rating at
        # different times.
        new_contest_ids = []
        contests_to_refetch = []  # List of (id, set of saved rated problem indices) pairs.
        if force_fetch:
            new_contest_ids = [contest.id for contest in contests]
        else:
            now = time.time()
            for contest in contests:
                if now > contest.end_time + self._MONITOR_PERIOD_SINCE_CONTEST_END:
                    # Contest too old, we do not want to check it.
                    continue
                problemset = self.cache_master.conn.fetch_problemset(contest.id)
                if not problemset:
                    new_contest_ids.append(contest.id)
                    continue
                rated_problem_idx = {prob.index for prob in problemset if prob.rating is not None}
                if len(rated_problem_idx) < len(problemset):
                    contests_to_refetch.append((contest.id, rated_problem_idx))

        new_problems, updated_problems = [], []
        for contest_id in new_contest_ids:
            new_problems += await self._fetch_for_contest(contest_id)
        for contest_id, rated_problem_idx in contests_to_refetch:
            updated_problems += [prob for prob in await self._fetch_for_contest(contest_id)
                                 if prob.rating is not None and prob.index not in rated_problem_idx]

        return new_problems, updated_problems

    async def _fetch_for_contest(self, contest_id):
        try:
            _, problemset, _ = await cf.contest.standings(contest_id=contest_id, from_=1,
                                                          count=1)
        except cf.CodeforcesApiError as er:
            self.logger.warning(f'Problemset fetch failed for contest {contest_id}. {er!r}')
            problemset = []
        return problemset

    def _save_problems(self, problems):
        rc = self.cache_master.conn.cache_problemset(problems)
        self.logger.info(f'Saved {rc} problems to database.')

    def get_problemset(self, contest_id):
        problemset = self.cache_master.conn.fetch_problemset(contest_id)
        if not problemset:
            raise ProblemsetNotCached(contest_id)
        return problemset


class RatingChangesCache:
    DEFAULT_RATING = 1500
    _RATED_DELAY = 36 * 60 * 60
    _RELOAD_DELAY = 10 * 60

    def __init__(self, cache_master):
        self.cache_master = cache_master
        self.monitored_contests = []
        self.handle_rating_cache = {}
        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self):
        self._refresh_handle_cache()
        if not self.handle_rating_cache:
            self.logger.warning('Rating changes cache on disk is empty. This must be populated '
                                'manually before use.')
        self._update_task.start()

    async def fetch_contest(self, contest_id):
        """Fetch rating changes for a particular contest. Intended for manual trigger."""
        contest = self.cache_master.contest_cache.contest_by_id[contest_id]
        changes = await self._fetch([contest])
        self.cache_master.conn.clear_rating_changes(contest_id=contest_id)
        self._save_changes(changes)
        return len(changes)

    async def fetch_all_contests(self):
        """Fetch rating changes for all contests. Intended for manual trigger."""
        contests = self.cache_master.contest_cache.contests_by_phase['FINISHED']
        changes = await self._fetch(contests)
        self.cache_master.conn.clear_rating_changes()
        self._save_changes(changes)
        return len(changes)

    async def fetch_missing_contests(self):
        """Fetch rating changes for contests which are not saved in database. Intended for
        manual trigger."""
        contests = self.cache_master.contest_cache.contests_by_phase['FINISHED']
        contests = [contest for contest in contests if not self.has_rating_changes_saved(contest.id)]
        changes = await self._fetch(contests)
        self._save_changes(changes)
        return len(changes)

    def is_newly_finished_without_rating_changes(self, contest):
        now = time.time()
        return (contest.phase == 'FINISHED' and
                now - contest.end_time < self._RATED_DELAY and
                not self.has_rating_changes_saved(contest.id))

    @tasks.task_spec(name='RatingChangesCacheUpdate',
                     waiter=tasks.Waiter.for_event(events.ContestListRefresh))
    async def _update_task(self, _):
        # Some notes:
        # A hack phase is tagged as FINISHED with empty list of rating changes. After the hack
        # phase, the phase changes to systest then again FINISHED. Since we cannot differentiate
        # between the two FINISHED phases, we are forced to fetch during both.
        # A contest also has empty list if it is unrated. We assume that is the case if
        # _RATED_DELAY time has passed since the contest end.

        to_monitor = [contest for contest in self.cache_master.contest_cache.contests_by_phase['FINISHED']
                      if self.is_newly_finished_without_rating_changes(contest)]
        cur_ids = {contest.id for contest in self.monitored_contests}
        new_ids = {contest.id for contest in to_monitor}
        if new_ids != cur_ids:
            await self._monitor_task.stop()
            if to_monitor:
                self.monitored_contests = to_monitor
                self._monitor_task.start()
            else:
                self.monitored_contests = []

    @tasks.task_spec(name='RatingChangesCacheUpdate.MonitorNewlyFinishedContests',
                     waiter=tasks.Waiter.fixed_delay(_RELOAD_DELAY))
    async def _monitor_task(self, _):
        self.monitored_contests = [contest for contest in self.monitored_contests
                                   if self.is_newly_finished_without_rating_changes(contest)]
        if not self.monitored_contests:
            self.logger.info('Rated changes fetched for contests that were being monitored.')
            await self._monitor_task.stop()
            return

        all_changes = await self._fetch(self.monitored_contests)
        self._save_changes(all_changes)

    async def _fetch(self, contests):
        all_changes = []
        for contest in contests:
            try:
                changes = await cf.contest.ratingChanges(contest_id=contest.id)
                self.logger.info(f'{len(changes)} rating changes fetched for contest {contest.id}')
                all_changes += changes
            except cf.CodeforcesApiError as er:
                self.logger.warning(f'Fetch rating changes failed for contest {contest.id}, ignoring. {er!r}')
                pass
        return all_changes

    def _save_changes(self, changes):
        if not changes:
            return
        rc = self.cache_master.conn.save_rating_changes(changes)
        self.logger.info(f'Saved {rc} changes to database.')
        self._refresh_handle_cache()

    def _refresh_handle_cache(self):
        changes = self.cache_master.conn.get_all_rating_changes()
        handle_rating_cache = {}
        for change in changes:
            delta = change.newRating - change.oldRating
            try:
                handle_rating_cache[change.handle] += delta
            except KeyError:
                handle_rating_cache[change.handle] = self.DEFAULT_RATING + delta
        self.handle_rating_cache = handle_rating_cache
        self.logger.info(f'Ratings for {len(handle_rating_cache)} handles cached')

    def get_users_with_more_than_n_contests(self, time_cutoff, n):
        return self.cache_master.conn.get_users_with_more_than_n_contests(time_cutoff, n)

    def get_rating_changes_for_contest(self, contest_id):
        return self.cache_master.conn.get_rating_changes_for_contest(contest_id)

    def has_rating_changes_saved(self, contest_id):
        return self.cache_master.conn.has_rating_changes_saved(contest_id)

    def get_rating_changes_for_handle(self, handle):
        return self.cache_master.conn.get_rating_changes_for_handle(handle)

    def get_current_rating(self, handle):
        return self.handle_rating_cache.get(handle)

    def get_current_rating_or_default(self, handle):
        return self.handle_rating_cache.get(handle, self.DEFAULT_RATING)

    def get_all_ratings(self):
        return list(self.handle_rating_cache.values())


class RanklistCacheError(CacheError):
    pass


class RanklistNotMonitored(RanklistCacheError):
    def __init__(self, contest):
        super().__init__(f'The ranklist for `{contest.name}` is not being monitored')
        self.contest = contest


class RanklistCache:
    _RELOAD_DELAY = 2 * 60

    def __init__(self, cache_master):
        self.cache_master = cache_master
        self.monitored_contests = []
        self.ranklist_by_contest = {}
        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self):
        self._update_task.start()

    def get_ranklist(self, contest):
        try:
            return self.ranklist_by_contest[contest.id]
        except KeyError:
            raise RanklistNotMonitored(contest)

    @tasks.task_spec(name='RanklistCacheUpdate',
                     waiter=tasks.Waiter.for_event(events.ContestListRefresh))
    async def _update_task(self, _):
        contests_by_phase = self.cache_master.contest_cache.contests_by_phase
        running_contests = contests_by_phase['_RUNNING']
        check = self.cache_master.rating_changes_cache.is_newly_finished_without_rating_changes
        to_monitor = running_contests + list(filter(check, contests_by_phase['FINISHED']))
        cur_ids = {contest.id for contest in self.monitored_contests}
        new_ids = {contest.id for contest in to_monitor}
        if new_ids != cur_ids:
            await self._monitor_task.stop()
            if to_monitor:
                self.monitored_contests = to_monitor
                self._monitor_task.start()
            else:
                self.ranklist_by_contest = {}

    @tasks.task_spec(name='RanklistCacheUpdate.MonitorActiveContests',
                     waiter=tasks.Waiter.fixed_delay(_RELOAD_DELAY))
    async def _monitor_task(self, _):
        check = self.cache_master.rating_changes_cache.is_newly_finished_without_rating_changes
        self.monitored_contests = [contest for contest in self.monitored_contests
                                   if contest.phase != 'FINISHED' or check(contest)]
        if not self.monitored_contests:
            self.ranklist_by_contest = {}
            self.logger.info('No more active contests for which to monitor ranklists.')
            await self._monitor_task.stop()
            return

        ranklist_by_contest = await self._fetch(self.monitored_contests)
        # If any ranklist could not be fetched, the old ranklist is kept.
        for contest_id, ranklist in ranklist_by_contest.items():
            self.ranklist_by_contest[contest_id] = ranklist

    async def generate_ranklist(self, contest_id, *, fetch_changes=False, predict_changes=False):
        assert fetch_changes ^ predict_changes

        contest, problems, standings = await cf.contest.standings(contest_id=contest_id,
                                                                  show_unofficial=True)
        now = time.time()

        # Exclude PRACTICE and MANAGER
        standings = [row for row in standings
                     if row.party.participantType in ('CONTESTANT', 'OUT_OF_COMPETITION', 'VIRTUAL')]

        if fetch_changes:
            # Fetch final rating changes from CF.
            # For older contests.
            is_rated = False
            try:
                changes = await cf.contest.ratingChanges(contest_id=contest_id)
                # For contests intended to be rated but declared unrated, an empty list is returned.
                is_rated = len(changes) > 0
            except cf.RatingChangesUnavailableError:
                pass
            ranklist = Ranklist(contest, problems, standings, now, is_rated=is_rated)
            if is_rated:
                delta_by_handle = {change.handle: change.newRating - change.oldRating
                                   for change in changes}
                ranklist.set_deltas(delta_by_handle)
        elif predict_changes:
            # Rating changes have not been applied yet, predict rating changes.
            # For running/recent contests.
            _, _, standings_official = await cf.contest.standings(contest_id=contest_id)

            has_teams = any(row.party.teamId is not None for row in standings_official)
            if cf_common.is_nonstandard_contest(contest) or has_teams:
                # The contest is not rated
                ranklist = Ranklist(contest, problems, standings, now, is_rated=False)
            else:
                get_rating = self.cache_master.rating_changes_cache.get_current_rating_or_default
                current_rating = {row.party.members[0].handle: get_rating(row.party.members[0].handle)
                                  for row in standings_official}
                if 'Educational' in contest.name:
                    # For some reason educational contests return all contestants in ranklist even
                    # when unofficial contestants are not requested.
                    current_rating = {handle: rating
                                      for handle, rating in current_rating.items() if rating < 2100}
                ranklist = Ranklist(contest, problems, standings, now, is_rated=True)
                ranklist.predict(current_rating)

        return ranklist

    async def _fetch(self, contests):
        ranklist_by_contest = {}
        for contest in contests:
            try:
                ranklist = await self.generate_ranklist(contest.id, predict_changes=True)
                ranklist_by_contest[contest.id] = ranklist
                self.logger.info(f'Ranklist fetched for contest {contest.id}')
            except cf.CodeforcesApiError as er:
                self.logger.warning(f'Ranklist fetch failed for contest {contest.id}. {er!r}')

        return ranklist_by_contest


class CacheSystem:
    def __init__(self, conn):
        self.conn = conn
        self.contest_cache = ContestCache(self)
        self.problem_cache = ProblemCache(self)
        self.rating_changes_cache = RatingChangesCache(self)
        self.ranklist_cache = RanklistCache(self)
        self.problemset_cache = ProblemsetCache(self)

    async def run(self):
        await self.rating_changes_cache.run()
        await self.ranklist_cache.run()
        await self.contest_cache.run()
        await self.problem_cache.run()
        await self.problemset_cache.run()
