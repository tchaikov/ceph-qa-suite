import contextlib
import logging
import os
import unittest
from unittest import suite, loader
from teuthology.task import interactive
from tasks.cephfs.filesystem import Filesystem


log = logging.getLogger(__name__)


class DecoratingLoader(loader.TestLoader):
    """
    A specialization of TestLoader that tags some extra attributes
    onto test classes as they are loaded.
    """
    def __init__(self, params):
        self._params = params
        super(DecoratingLoader, self).__init__()

    def loadTestsFromTestCase(self, testCaseClass):
        for k, v in self._params.items():
            setattr(testCaseClass, k, v)
        return super(DecoratingLoader, self).loadTestsFromTestCase(testCaseClass)


class LogStream(object):
    def __init__(self):
        self.buffer = ""

    def write(self, data):
        self.buffer += data
        if "\n" in self.buffer:
            lines = self.buffer.split("\n")
            for line in lines[:-1]:
                log.info(line)
            self.buffer = lines[-1]

    def flush(self):
        pass


class InteractiveFailureResult(unittest.TextTestResult):
    """
    Specialization that implements interactive-on-error style
    behavior.
    """
    ctx = None

    def addFailure(self, test, err):
        log.error(self._exc_info_to_string(err, test))
        log.error("Failure in test '{0}', going interactive".format(
            self.getDescription(test)
        ))
        interactive.task(ctx=self.ctx, config=None)

    def addError(self, test, err):
        log.error(self._exc_info_to_string(err, test))
        log.error("Error in test '{0}', going interactive".format(
            self.getDescription(test)
        ))
        interactive.task(ctx=self.ctx, config=None)


@contextlib.contextmanager
def task(ctx, config):
    fs = Filesystem(ctx)

    # Mount objects, sorted by ID
    mounts = [v for k, v in sorted(ctx.mounts.items(), lambda a, b: cmp(a[0], b[0]))]

    decorating_loader = DecoratingLoader({
        "ctx": ctx,
        "mounts": mounts,
        "fs": fs
    })

    # Put useful things onto ctx for interactive debugging
    ctx.fs = fs

    # Depending on config, either load specific modules, or scan for moduless
    if config and 'modules' in config and config['modules']:
        module_suites = []
        for mod_name in config['modules']:
            # Test names like cephfs.test_auto_repair
            module_suites.append(decorating_loader.loadTestsFromName(mod_name))
        overall_suite = suite.TestSuite(module_suites)
    else:
        # Default, run all tests
        overall_suite = decorating_loader.discover(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "cephfs/"
            )
        )

    if ctx.config.get("interactive-on-error", False):
        InteractiveFailureResult.ctx = ctx
        result_class = InteractiveFailureResult
    else:
        result_class = unittest.TextTestResult

    class LoggingResult(result_class):
        def startTest(self, test):
            log.info("Starting test: {0}".format(self.getDescription(test)))
            return super(LoggingResult, self).startTest(test)

    # Execute!
    result = unittest.TextTestRunner(
        stream=LogStream(),
        resultclass=LoggingResult,
        verbosity=2,
        failfast=True).run(overall_suite)

    if not result.wasSuccessful():
        result.printErrors()  # duplicate output at end for convenience

        bad_tests = []
        for test, error in result.errors:
            bad_tests.append(str(test))
        for test, failure in result.failures:
            bad_tests.append(str(test))

        raise RuntimeError("Test failure: {0}".format(", ".join(bad_tests)))

    yield
