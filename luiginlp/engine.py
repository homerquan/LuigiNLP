import sys
import os
import luigi
import sciluigi
import logging
import inspect
import argparse
import importlib
import itertools
import shutil
import subprocess
import glob
import socket
import json
from luiginlp.util import shellsafe, getlog, replaceextension

log = getlog()

INPUTFORMATS = []
COMPONENTS = []

def registerformat(Class):
    assert inspect.isclass(Class) and issubclass(Class,InputFormat)
    if Class not in INPUTFORMATS:
        INPUTFORMATS.append(Class)
    return Class

def registercomponent(Class):
    assert inspect.isclass(Class) and issubclass(Class,WorkflowComponent)
    if Class not in COMPONENTS:
        COMPONENTS.append(Class)
    return Class

class LuigiNLPException(Exception):
    pass

class InvalidInput(LuigiNLPException):
    pass

class MissingInput(LuigiNLPException):
    pass

class EmptyDirectory(LuigiNLPException):
    pass

class AutoSetupError(LuigiNLPException):
    pass

class SchedulingError(LuigiNLPException):
    pass

class InputComponent:
    """A class that encapsulates a WorkflowComponent and is used by other components to list possible dependencies, used in WorkflowComponent.accepts(), holds parameter information to pass to sub-workflows"""
    def __init__(self, parentcomponent, Class, *args,**kwargs):
        assert inspect.isclass(Class) and issubclass(Class,WorkflowComponent)
        self.Class = Class
        self.args = args
        self.kwargs = kwargs
        #automatically transfer parameters
        for key in dir(self.Class):
            attr = getattr(self.Class, key)
            if isinstance(attr,luigi.Parameter) and key not in self.kwargs and hasattr(parentcomponent ,key):
                self.kwargs[key] = getattr(parentcomponent, key)

class InputTask(sciluigi.ExternalTask):
    """InputTask, an external task"""

    format_id=luigi.Parameter()
    basename = luigi.Parameter()
    extension = luigi.Parameter()
    directory = luigi.BoolParameter()

    def out_default(self):
        return TargetInfo(self, self.basename + '.' + self.extension)



class InputFormat:
    """A class that encapsulates an initial task"""

    def __init__(self, workflow, format_id, extension, inputparameter='inputfile', directory=False, force=False):
        assert isinstance(workflow,WorkflowComponent)
        self.inputtask = None
        self.valid = False
        self.format_id = format_id
        self.directory = directory
        if isinstance(extension, str):
            extensions = (extension,)
        else:
            extensions = extension
        if not hasattr(workflow,inputparameter):
            raise AttributeError("Workflow " + workflow.__class__.__name__ + " has no attribute " + inputparameter)
        for extension in extensions:
            if extension[0] == '.': extension = extension[1:]
            if getattr(workflow,inputparameter).endswith('.' + extension) or force:
                self.basename =  getattr(workflow,inputparameter)[:-(len(extension) + 1)]
                self.extension = extension
                if not os.path.exists(self.basename + '.' + self.extension):
                    raise FileNotFoundError("Specified input file for format " + self.format_id + " to " + workflow.__class__.__name__ + " does not exist: " + self.basename + "." + self.extension)
                self.valid = True
                break
        #if not self.valid:
        #    log.info("InputFormat " + format_id + " (extensions " + ",".join(extensions)+") did not match " + getattr(workflow, inputparameter))

    def __str__(self):
        if self.valid:
            return self.basename + '.' + self.extension
        else:
            return ""

    def task(self, workflow):
        if self.valid:
            return workflow.new_task('inputtask_' + self.format_id, InputTask, basename=self.basename, format_id=self.format_id,extension=self.extension, directory=self.directory)
        else:
            raise Exception("Can't produce task for an invalid inputformat!")



class WorkflowComponent(sciluigi.WorkflowTask):
    """A workflow component"""

    startcomponent = luigi.Parameter(default="")
    inputslot = luigi.Parameter(default="")

    accepted_components = [] #additional accepted components (will be injected through the accept() method)


    def requires(self):
        '''
        Implementation of Luigi API method. (Overrides SciLuigi with minor modification!)
        '''
        if not self._hasaddedhandler:
            self._hasaddedhandler = True
        clsname = self.__class__.__name__
        if not self._hasloggedstart:
            log.info('>>> Starting component %s', str(self)) # (logging to %s)', clsname, self.get_wflogpath())
            self._hasloggedstart = True
        workflow_output = self.workflow()
        if workflow_output is None:
            clsname = self.__class__.__name__
            raise Exception(('Nothing returned from workflow() method in the %s Workflow task. '
                             'Forgot to add a return statement at the end?') % clsname)
        return workflow_output

    def run(self):
        if not self._hasloggedfinish:
            clsname = self.__class__.__name__
            log.info('<<< Finished component %s', str(self))
            self._hasloggedfinish = True
        return super().run()

    def output(self):
        return {'audit': luigi.LocalTarget(self.get_auditlogpath())}


    @classmethod
    def accept(cls, *ChildClasses):
        for ChildClass in ChildClasses:
            if ChildClass not in cls.accepted_components:
                cls.accepted_components.append(ChildClass)

    @classmethod
    def inherit_parameters(cls, *ChildClasses):
        for ChildClass in ChildClasses:
            for key in dir(ChildClass):
                if key not in ('instance_name', 'workflow_task'):
                    attr = getattr(ChildClass, key)
                    if isinstance(attr,luigi.Parameter) and not hasattr(cls,key):
                        setattr(cls,key, attr)

    def setup(self,workflow, input_feeds):
        if hasattr(self, 'autosetup'):
            input_feeds = self.setup_input(workflow)
            if len(input_feeds) > 1:
                #print("Input feed from "  + self.__class__.__name__ + ": ", len(input_feeds), repr(input_feeds),file=sys.stderr)
                raise AutoSetupError("Autosetup only works for single input/output tasks for now")
            configuration = self.autosetup()
            input_type, input_slot = list(input_feeds.items())[0]
            if not isinstance(configuration, (list, tuple)): configuration = (configuration,)
            for TaskClass in configuration:
                if not inspect.isclass(TaskClass) or not issubclass(TaskClass,Task):
                    raise AutoSetupError("AutoSetup expected a Task class, got " + str(type(TaskClass)))
                if hasattr(TaskClass, 'in_' + input_type):
                    passparameters = {}
                    for key in dir(TaskClass):
                        if key not in ('instance_name', 'workflow_task') and isinstance(getattr(TaskClass,key), luigi.Parameter):
                            if hasattr(self, key):
                                passparameters[key] = getattr(self,key)
                    task = workflow.new_task(TaskClass.__name__, TaskClass,**passparameters)
                    setattr(task, 'in_' + input_type, input_slot)
                    found = False
                    for key in dir(TaskClass):
                        if key.startswith('out_'):
                            found = True
                    if not found:
                        raise AutoSetupError("No output slots found on " + TaskClass.__name__)
                    else:
                        return task
            raise AutoSetupError("No matching input slots found for the specified task (looking for " + input_type + " on " + TaskClass.__name__ + ")")
        else:
            raise NotImplementedError("Override the setup() or autosetup() method for your workflow component " + self.__class__.__name__)

    def setup_input(self, workflow):
        if inspect.isclass(self.startcomponent): self.startcomponent = self.startcomponent.__name__
        #Can we handle the input directly?
        accepts = self.accepts()
        inputlog = []
        if hasattr(self, 'inputfile'):
            inputlog.append("inputfile=" + self.inputfile)
        if not isinstance(accepts, (tuple, list)):
            accepts = (accepts,)
        for inputtuple in itertools.chain(accepts, self.accepted_components):
            input_feeds = {} #reset
            if not isinstance(inputtuple, tuple): inputtuple = (inputtuple,)
            for input in inputtuple: #pylint: disable=redefined-builtin
                if isinstance(input, InputFormat):
                    if self.startcomponent and self.startcomponent != self.__class__.__name__:
                        inputlog.append("startcomponent does not match " + self.__class__.__name__ + ", skipping InputFormat " + input.format_id)
                        break
                    if input.valid and (not self.inputslot or self.inputslot == input.format_id):
                        inputlog.append("InputFormat " + input.format_id + " matches!")
                        input_feeds[input.format_id] = input.task(workflow).out_default
                        #print("UPDATED INPUT_FEEDS (a)", len(input_feeds), repr(input_feeds),file=sys.stderr)
                        continue
                    else:
                        inputlog.append("InputFormat " + input.format_id + " does not match (inputslot=" + self.inputslot+")")
                        #print("BREAKING INPUT_FEEDS (a)",file=sys.stderr)
                        break
                elif isinstance(input, InputComponent):
                    swf = input.Class(*input.args, **input.kwargs)
                elif inspect.isclass(input) and issubclass(input, WorkflowComponent):
                    #not encapsulated in InputWorkflow yet, do now
                    iwf = InputComponent(self, input)
                    swf = iwf.Class(*input.args, **input.kwargs)
                else:
                    raise TypeError("Invalid element in accepts(), must be Inputformat or InputComponent, got " + str(repr(input)))

                try:
                    new_input_feeds = swf.setup_input(workflow)
                    inputtasks = swf.setup(workflow, new_input_feeds)
                    #print("SUBWORKFLOW INPUT_FEEDS (b)",len(new_input_feeds), repr(new_input_feeds),file=sys.stderr)
                except InvalidInput as e:
                    inputlog.append( "(Tried workflow " + swf.__class__.__name__ + " in accept chain, does not handle provided input: " + str(e) + ")")
                    log.debug("(Tried workflow " + swf.__class__.__name__ + " in accept chain, does not handle provided input)")
                    #print("SUBWORKFLOW INVALID INPUT (b)", file=sys.stderr)
                    break

                log.debug("Workflow " + swf.__class__.__name__ + " handles the provided input")

                if isinstance(inputtasks, Task): inputtasks = (inputtasks,)
                for inputtask in inputtasks:
                    if not isinstance(inputtask, Task):
                        raise TypeError("setup() did not return a Task or a sequence of Tasks")
                    for attrname in dir(inputtask):
                        if attrname[:4] == 'out_':
                            format_id = attrname[4:]
                            if format_id in input_feeds:
                                if isinstance(input_feeds[format_id], list):
                                    input_feeds[format_id] += [getattr(inputtask, attrname)]
                                else:
                                    input_feeds[format_id] = [input_feeds[format_id], getattr(inputtask, attrname)]
                            else:
                                input_feeds[format_id] = getattr(inputtask, attrname)

                #print("UPDATED INPUT_FEEDS (c)",len(input_feeds), repr(input_feeds),file=sys.stderr)

            if len(input_feeds) > 0:
                #print("RETURNING INPUT_FEEDS (d)",len(input_feeds), repr(input_feeds),file=sys.stderr)
                return input_feeds

        #input was not handled, raise error
        raise InvalidInput("Unable to find an entry point for supplied input: " + "; ". join(inputlog))

    def workflow(self):
        try:
            input_feeds = self.setup_input(self)
            output_task = self.setup(self, input_feeds)
        except Exception as e:
            log.error(e.__class__.__name__ + ": " + str(e))
            raise
        if output_task is None or not (isinstance(output_task, Task) or (isinstance(output_task, (list,tuple)) and all([isinstance(output_task, Task) for t in output_task]))):
            raise ValueError("Workflow setup() did not return a valid last task (or sequence of tasks), got " + str(type(output_task)))
        return output_task

    def new_task(self, instance_name, cls, **kwargs):
        #automatically inherit parameters
        if not isinstance(instance_name,str):
            raise TypeError("First parameter to new_task must be an instance_name (str), got " + repr(instance_name))
        if 'autopass' in kwargs and kwargs['autopass']:
            for key in dir(cls):
                if key not in ('instance_name', 'workflow_task'):
                    attr = getattr(cls, key)
                    if isinstance(attr,luigi.Parameter) and key not in kwargs and hasattr(self,key):
                        kwargs[key] = getattr(self,key)
            del kwargs['autopass']
        return super().new_task(instance_name, cls, **kwargs)

class Task(sciluigi.Task):
    outputdir = luigi.Parameter(default="")

    def setup_output_dir(self, d):
        #Make output directory
        if os.path.exists(d):
            pass
        elif os.path.exists(d + '.failed'):
            os.rename(d +'.failed',d)
        else:
            os.makedirs(d)
        try:
            self.__output_dir.append(d)
        except AttributeError: #not defined yet:
            self.__output_dir = [d]

    def on_failure(self, exception):
        try:
            if self.__output_dir:
                for d in self.__output_dir:
                    if os.path.exists(d):
                        os.rename(d, d + '.failed')
        except AttributeError:
            pass
        return super().on_failure(exception)

    def on_success(self):
        try:
            if self.__output_dir:
                failed = []
                for d in self.__output_dirs:
                    if os.path.exists(self.__output_dir):
                        files = [ f for f in glob.glob(os.path.join(d,'*')) if f not in ('.','..') ]
                        if not files:
                            #an empty directory is not success
                            os.rename(d, d + '.failed')
                            failed.append(d)
                if failed:
                    raise EmptyDirectory("Target directory/directories " + ','.join(failed) + " is/are empty. Expected contents")
        except AttributeError:
            pass

        for attrname in dir(self):
            if attrname[:4] == 'out_':
                log.info("Produced output " + getattr(self, attrname)().path)
        return super().on_success()

    def run(self):
        raise NotImplementedError("No run() method implemented for Task " + self.__class__.__name__)

    def getcmd(self, *args, **kwargs):
        if not hasattr(self,'executable'):
            raise Exception("No executable defined for Task " + self.__class__.__name__)

        if self.executable[-4:] == '.jar':
            cmd = 'java -jar ' + self.executable
        else:
            cmd = self.executable
        opts = []
        for key, value in kwargs.items():
            if value is None or value is False:
                continue #no value, ignore this one
            if key.startswith('__'): #internal option: ignore
                continue
            if key.find('__') > 0:
                #rewrite double underscore to hyphen, for options like --foo-bar (foo__bar)
                key = key.replace('__','-')
            delimiter = ' '
            if key[0] == '_':
                key = key[1:]
            if '__nospace' in kwargs and kwargs['__nospace']:
                delimiter = ''
            if len(key) == 1 or ('__singlehyphen' in kwargs and kwargs['__singlehyphen']):
                key = '-' + key
            else:
                key = '--' + key
                if '__assignop' in kwargs and kwargs['__assignop']:
                    delimiter = '='

            if value is True:
                opts.append(key)
            elif isinstance(value,str):
                opts.append(key + delimiter + shellsafe(value))
            else:
                opts.append(key + delimiter + str(value))

        if '__options_last' in kwargs and kwargs['__options_last']:
            if args:
                cmd += ' ' + ' '.join(args)
            if opts:
                cmd += ' ' + ' '.join(opts)
        else:
            if opts:
                cmd += ' ' + ' '.join(opts)
            if args:
                cmd += ' ' + ' '.join(args)

        if '__stdin_from' in kwargs:
            cmd += ' < ' + shellsafe(kwargs['__stdin_from'])
        if '__stdout_to' in kwargs:
            cmd += ' > ' + shellsafe(kwargs['__stdout_to'])
        if '__stderr_to' in kwargs:
            cmd += ' 2> ' + shellsafe(kwargs['__stderr_to'])

        return cmd


    def ex(self, *args, **kwargs):
        cmd = self.getcmd(*args,**kwargs)
        if '__ignorefailure' in kwargs and kwargs['__ignorefailure']:
            try:
                super(Task, self).ex(cmd)
            except:
                log.warn("Ignoring failure on request!")
                pass
        else:
            super(Task, self).ex(cmd)


    def ex_async(self, *args, **kwargs):
        cmd = self.getcmd(*args,**kwargs)
        process = subprocess.Popen(cmd, shell=True)
        if process:
            log.info("Executing asynchronous command: " + cmd)
            return process.pid
        else:
            raise Exception("Unable to launch process: " + cmd)




    @classmethod
    def inherit_parameters(Class, *ChildClasses):
        for ChildClass in ChildClasses:
            for key in dir(ChildClass):
                if key not in ('instance_name', 'workflow_task'):
                    attr = getattr(ChildClass, key)
                    if isinstance(attr,luigi.Parameter) and not hasattr(Class, key):
                        setattr(Class,key, attr)

    def outputfrominput(self, inputformat, stripextension, addextension, replaceinputdirparam='replaceinputdir', outputdirparam='outputdir'):
        """Derives the output filename from the input filename, removing the input extension and adding the output extension. Supports outputdir parameter."""

        if not hasattr(self,'in_' + inputformat):
            raise ValueError("Specified inputslot for " + inputformat + " does not exist in " + self.__class__.__name__)
        inputslot = getattr(self, 'in_' + inputformat)

        try:
            inputfilename = inputslot().path
        except (AttributeError, TypeError):
            raise ValueError("Inputslot in_" + inputformat + " of " + self.__class__.__name__ + " is not connected to any output slot!")

        if hasattr(self,outputdirparam):
            outputdir = getattr(self,outputdirparam)
            if outputdir and outputdir != '.':
                if hasattr(self, replaceinputdirparam):
                    replaceinputdir = getattr(self,replaceinputdirparam)
                else:
                    replaceinputdir = None
                if replaceinputdir:
                    if inputfilename.startswith(replaceinputdir):
                        return TargetInfo(self, os.path.join(outputdir, os.path.basename(replaceextension(inputfilename[len(replaceinputdir):], stripextension,addextension))))
                else:
                    return TargetInfo(self, os.path.join(outputdir, os.path.basename(replaceextension(inputfilename, stripextension,addextension))))
            else:
                return TargetInfo(self, replaceextension(inputfilename, stripextension,addextension))


class StandardWorkflowComponent(WorkflowComponent):
    """A workflow component that takes one inputfile"""

    inputfile = luigi.Parameter()
    outputdir = luigi.Parameter(default="")
    replaceinputdir = luigi.Parameter(default="")

class TargetInfo(sciluigi.TargetInfo):
    pass


def getcomponentclass(classname):
    for Class in COMPONENTS:
        if Class.__name__ == classname:
            return Class
    raise Exception("No such component: " + classname)

class PassParameters(dict):
    def __init__(self, *args, **kwargs):
        super().__init__()
        if args:
            self.update(args[0])
        if kwargs:
            self.update(kwargs)

    def __hash__(self):
        return hash(tuple(sorted(self.items())))

class ParallelBatch(luigi.Task):
    """Meta workflow"""
    inputfiles = luigi.Parameter()
    component = luigi.Parameter()
    passparameters = luigi.Parameter(default=PassParameters())

    def requires(self):
        if isinstance(self.passparameters, str):
            self.passparameters = PassParameters(json.loads(self.passparameters.replace("'",'"')))
        elif isinstance(self.passparameters, dict):
            self.passparameters = PassParameters(self.passparameters)
        elif not isinstance(self.passparameters, PassParameters):
            raise TypeError("Keywork argument passparameters must be instance of PassParameters, got " + repr(self.passparameters))
        tasks = []
        ComponentClass = getcomponentclass(self.component)
        if isinstance(self.inputfiles, str):
            self.inputfiles = self.inputfiles.split(',')
        for inputfile in self.inputfiles:
            tasks.append(  ComponentClass(inputfile=inputfile,**self.passparameters))
        return tasks

    def run(self):
        if isinstance(self.inputfiles, str):
            self.inputfiles = self.inputfiles.split(',')
        with self.output().open('w') as f:
            f.write("\n".join(self.inputfiles))

    def output(self):
        return luigi.LocalTarget('.parallelbatch-' + self.component + '-' + str(hash(self)) + '.done')

class Parallel(sciluigi.WorkflowTask):
    """Meta workflow"""
    inputfiles = luigi.Parameter()
    component = luigi.Parameter()
    passparameters = luigi.Parameter(default=PassParameters())

    def workflow(self):
        if isinstance(self.passparameters, str):
            self.passparameters = PassParameters(json.loads(self.passparameters.replace("'",'"')))
        elif isinstance(self.passparameters, dict):
            self.passparameters = PassParameters(self.passparameters)
        elif not isinstance(self.passparameters, PassParameters):
            raise TypeError("Keywork argument passparameters must be instance of PassParameters, got " + repr(self.passparameters))
        tasks = []
        ComponentClass = getcomponentclass(self.component)
        if isinstance(self.inputfiles, str):
            self.inputfiles = self.inputfiles.split(',')
        for inputfile in self.inputfiles:
            tasks.append( self.new_task(self.component, ComponentClass, inputfile=inputfile,**self.passparameters) )
        return tasks

class ParallelFromDir(sciluigi.WorkflowTask):
    """Meta Workflow"""
    directory = luigi.Parameter()
    pattern = luigi.Parameter(default="*")
    component = luigi.Parameter()
    passparameters = luigi.Parameter(default=PassParameters())

    def workflow(self):
        if isinstance(self.passparameters, str):
            self.passparameters = PassParameters(json.loads(self.passparameters.replace("'",'"')))
        elif isinstance(self.passparameters, dict):
            self.passparameters = PassParameters(self.passparameters)
        elif not isinstance(self.passparameters, PassParameters):
            raise TypeError("Keywork argument passparameters must be instance of PassParameters, got " + repr(self.passparameters))
        tasks = []
        ComponentClass = getcomponentclass(self.component)
        for inputfile in glob.glob(os.path.join(self.directory, self.pattern)):
            tasks.append( self.new_task(self.component, ComponentClass, inputfile=inputfile,**self.passparameters) )
        return tasks

def run(*args, **kwargs):
    luigi_logger = logging.getLogger('luigi-interface')
    logfile = luigi_logger.handlers[0].baseFilename
    if len(luigi_logger.handlers) == 2:
        luigi_logger.removeHandler(luigi_logger.handlers[1]) #ugly patch remove the stream handler for the luigi-interface log that sciluigi configured
    luigi_logger.setLevel(logging.INFO)

    log.info("LuigiNLP: Starting workflow (logging to %s)",logfile )

    if 'scheduler_host' in kwargs:
        host = kwargs['scheduler_host']
    else:
        host = 'localhost'

    if 'scheduler_port' in kwargs:
        port = kwargs['scheduler_port']
    else:
        port = 8082

    #test whether luigid is running, fall back to local scheduler otherwise
    if 'local_scheduler' not in kwargs:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((host,port))
            log.info("Using scheduler at " + host + ":" + str(port))
        except:
            kwargs['local_scheduler'] = True
            log.info("Using local scheduler")

    if not args:
        success = luigi.run(**kwargs)
    else:
        success = luigi.build(args,**kwargs)

    if not success:
        log.error("LuigiNLP: There were errors in scheduling the workflow, inspect the log at %s for more details", logfile)
    else:
        log.info("LuigiNLP: Workflow run completed succesfully (logged to %s)", logfile)
    return success

def run_cmdline(TaskClass,**kwargs):
    if 'local_scheduler' in kwargs:
        local_scheduler = kwargs['local_scheduler']
    else:
        local_scheduler=True
    if 'module' in kwargs:
        importlib.import_module(kwargs['module'])
        del kwargs['module']
    cmdline_args = []
    for key, value in kwargs.items():
        if inspect.isclass(value):
            value = value.__name__
        cmdline_args.append('--' + key + ' ' + str(shellsafe(value)))
    kwargs = {}
    if local_scheduler:
        kwargs['local_scheduler'] = True
    luigi.run(main_task_cls=TaskClass,cmdline_args=' '.join(cmdline_args), **kwargs)

def InputSlot():
    return lambda: None

class Parameter(sciluigi.Parameter):
    pass

class BoolParameter(luigi.BoolParameter):
    pass

class IntParameter(luigi.IntParameter):
    pass

class FloatParameter(luigi.FloatParameter):
    pass
