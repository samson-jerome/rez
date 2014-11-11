from rez.solver import Solver, SolverStatus, PackageVariantCache
from rez.vendor.enum import Enum


class ResolverStatus(Enum):
    """ Enum to represent the current state of a resolver instance.  The enum
    also includes a human readable description of what the state represents.
    """

    pending = ("The resolve has not yet started.", )
    solved = ("The resolve has completed successfully.", )
    failed = ("The resolve is not possible.", )
    aborted = ("The resolve was stopped by the user (via callback).", )

    def __init__(self, description):
        self.description = description


class Resolver(object):
    """The package resolver.

    The Resolver uses a combination of Solver(s) and cache(s) to resolve a
    package request as quickly as possible.
    """
    def __init__(self, package_requests, package_paths, caching=True,
                 timestamp=0, callback=None, building=False, verbosity=False,
                 buf=None, package_load_callback=None, max_depth=0,
                 start_depth=0):
        """Create a Resolver.

        Args:
            package_requests: List of Requirement objects representing the
                request.
            package_paths: List of paths to search for pkgs.
            caching: If True, utilise cache(s) in order to speed up the
                resolve.
            callback: See `Solver`.
            package_load_callback: If not None, this callable will be called
                prior to each package being loaded. It is passed a single
                `Package` object.
            building: True if we're resolving for a build.
            max_depth (int): If non-zero, this value limits the number of packages
                that can be loaded for any given package name. This effectively
                trims the search space - only the highest N package versions are
                searched.
            start_depth (int): If non-zero, an initial solve is performed with
                `max_depth` set to this value. If this fails, the depth is doubled,
                and another solve is performed. If `start_depth` is specified but
                `max_depth` is not, the solve will iterate until all relevant
                packages have been loaded. Using this argument  allows us to
                perform something like a breadth-first search - we put off
                loading older packages with the assumption that they aren't being
                used anymore.
        """
        self.package_requests = package_requests
        self.package_paths = package_paths
        self.caching = caching
        self.timestamp = timestamp
        self.callback = callback
        self.package_load_callback = package_load_callback
        self.building = building
        self.verbosity = verbosity
        self.buf = buf

        self.max_depth = max_depth
        self.start_depth = start_depth
        if self.max_depth and self.start_depth:
            assert self.max_depth >= self.start_depth

        self.status_ = ResolverStatus.pending
        self.resolved_packages_ = None
        self.failure_description = None
        self.graph_ = None

        self.solve_time = 0.0  # time spent solving
        self.load_time = 0.0   # time spent loading package resources

    def solve(self):
        """Perform the solve."""
        package_cache = PackageVariantCache(
            self.package_paths,
            package_requests=self.package_requests,
            timestamp=self.timestamp,
            package_load_callback=self.package_load_callback,
            building=self.building)

        kwargs = dict(package_requests=self.package_requests,
                      package_cache=package_cache,
                      package_paths=self.package_paths,
                      timestamp=self.timestamp,
                      callback=self.callback,
                      package_load_callback=self.package_load_callback,
                      building=self.building,
                      verbosity=self.verbosity,
                      buf=self.buf)

        if self.start_depth:
            # perform an iterative solve, doubling search depth until a solution
            # is found or all packages are exhausted
            depth = self.start_depth

            while True:
                solver = Solver(max_depth=depth, **kwargs)
                solver.pr.header("SOLVING TO DEPTH %d..." % depth)
                solver.solve()

                if not solver.is_partial \
                        or solver.status == SolverStatus.solved \
                        or self.max_depth and depth >= self.max_depth:
                    break
                else:
                    depth *= 2
                    if self.max_depth:
                        depth = min(depth, self.max_depth)

        elif self.max_depth:
            # perform a solve that loads only the first N packages of any
            # given package request in the solve
            solver = Solver(max_depth=self.max_depth, **kwargs)
            solver.solve()
        else:
            # perform a solve that loads all relevant packages
            solver = Solver(**kwargs)
            solver.solve()

        self._set_result(solver)

    @property
    def status(self):
        """Return the current status of the resolve.

        Returns:
          ResolverStatus.
        """
        return self.status_

    @property
    def resolved_packages(self):
        """Get the list of resolved packages.

        Returns:
            List of `PackageVariant` objects, or None if the resolve has not
            completed.
        """
        return self.resolved_packages_

    @property
    def graph(self):
        """Return the resolve graph.

        The resolve graph shows unsuccessful as well as successful resolves.

        Returns:
            A pygraph.digraph object, or None if the solve has not completed.
        """
        return self.graph_

    def _set_result(self, solver):
        st = solver.status
        pkgs = None

        if st == SolverStatus.unsolved:
            self.status_ = ResolverStatus.aborted
            self.failure_description = solver.abort_reason
        elif st == SolverStatus.failed:
            self.status_ = ResolverStatus.failed
            self.failure_description = solver.failure_description()
        elif st == SolverStatus.solved:
            self.status_ = ResolverStatus.solved
            pkgs = solver.resolved_packages

        self.resolved_packages_ = pkgs
        self.graph_ = solver.get_graph()
        self.solve_time = solver.solve_time
        self.load_time = solver.load_time
