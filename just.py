#!/usr/bin/python

import argparse
import sys
import re
import os.path
import subprocess
from subprocess import Popen

import IO

class Struct():
    pass


def parseArgs():
    parser = argparse.ArgumentParser()
    parser.add_argument('main', type=str)
    parser.add_argument('--workdir', type=str, required=True, help='working directory')
    parser.add_argument('--stages', '-s', default="-", type=str, help='1 or 1-5 (default all)')
    parser.add_argument('--evaluate', '-e', default=None, type=str, help="a bash command evaluated before each task")
    parser.add_argument('--bashheader', default="#!/bin/bash", type=str, help='bash header')
    parser.add_argument('--verbose', '-v', action='store_true', help="verbose bash files (-v)")
    parser.add_argument('--debug', '-x', action='store_true', help="debug bash files (-x)")
    parser.add_argument('--list', action='store_true', help="list which stages are available")
    parser.add_argument('--qsub', '-q', default=None, type=str, help='qsub queue (works for a single task only)')

    args = parser.parse_args()
    if args.verbose:
        args.bashheader += ' -v'
    if args.debug:
        args.bashheader += ' -x'

    return args


def parseTasks(args):
    lines = IO.readlines(args.main, strip=True, skip=['empty'])
    tasks = dict()
    is_in = False
    prev_id = None
    for line in lines:
        m_start = re.match(r"^(.*):\s*{{\s*$", line)
        m_end = re.match(r"^\s*}}\s*$", line)

        if m_start:
            is_in = True
            task_id = []
            task_name = []
            task_parents = []
            for attr in m_start.group(1).split(":"):
                try:
                    attr = int(attr)
                except ValueError:
                    pass

                if isinstance(attr, int):
                    task_id.append(attr)
                elif "=" in attr:
                    key, val = attr.split("=", 1)
                    if key.strip() == "parents":
                        if val == "none":
                            task_parents.append(None)
                        else:
                            task_parents.extend(map(int,val.split(",")))
                else:
                    task_name.append(attr.strip())

            if len(task_name) == 1:
                task_name = task_name[0]
            else:
                msg("ERROR: task must have exactly one name")
                sys.exit(1)
            if '-' in task_name:
                msg("WARNING: task name should not contain -")

            if len(task_id) == 1:
                task_id = task_id[0]
            else:
                msg("ERROR: task must have exactly one id")
                sys.exit(1)
            if task_id == prev_id:
                msg("ERROR: more than one task has id {}".format(task_id))
                sys.exit(1)
            elif task_id < prev_id:
                msg("ERROR: task ids not in increasing order")
                sys.exit(1)

            if len(task_parents) == 0 and prev_id is not None:
                task_parents = [prev_id] # this would be incorrect if tasks not in order
            elif None in task_parents: # so parents=none,123 still means no parents
                task_parents = []

            if task_id > 0: prev_id = task_id

            task_body = []

        elif m_end:
            is_in = False
            tasks[task_id] = {'task_name': task_name, 'task_body': task_body, 'task_parents': task_parents}

        elif is_in:
            task_body.append(line)

    return tasks


def write_body(cmd_args, prologue_body, task_id, task_name, task_body):
    # Files are saved in the working directory, under the name s$id.$name.sh
    # note 1: qsub fails on file names starting with a digit.
    # note 2: shorter names are better for qstat which displays the first 10 characters of the submitted script name
    path = "%s/s%d.%s.sh" % (cmd_args.workdir, task_id, task_name)
    with open(path, 'wb') as f:

        f.write(cmd_args.bashheader + '\n\n')                               # bash header

        if cmd_args.evaluate is not None:
            f.write('\n## From -e option ##\n')
            f.write(cmd_args.evaluate + '\n')

        # write the parsed config
        f.write('## CONFIG ##\n')
        for line in prologue_body:
            f.write(line + '\n')

        # write the commands.
        f.write('\n## BODY ##\n')
        for line in task_body:
            f.write(line + '\n')
    return path


MSG_PREFIX = "JUST:"
def msg(str):
    print >> sys.stderr, MSG_PREFIX, str


def lookupTask(task_id, tasks):
    try:
        task_id = int(task_id)
        if task_id not in tasks:
            raise ValueError("task {} not found".format(task_id))
        return task_id
    except ValueError:
        cands = [i for i in tasks if tasks[i]['task_name'] == task_id]
        if len(cands) == 0:
            raise ValueError("task {} not found".format(task_id))
        elif len(cands) > 1:
            raise ValueError("task name {} is ambiguous".format(task_id))
        return cands[0]

def getNoDependencyTasks(queue):
    tasks_to_run = []
    to_delete = []
    for child_id in queue:
        parents = queue[child_id]
        if len(parents) == 0:
            tasks_to_run.append(child_id)
            to_delete.append(child_id)
    for to_d in to_delete:
        del queue[to_d]
    return (queue, tasks_to_run)

def deleteInQueue(queue, task):
    for child_id in queue:
        parents = queue[child_id]
        if task in parents:
            parents.remove(task)
        queue[child_id] = parents
    return queue

def run_local(tasks, start, stop):
    # Create subgraph of dependencies
    queue = dict()
    for child_id in tasks:
        if start <= child_id <= stop:
            parents = tasks[child_id]['task_parents']
            range_parents = []
            for parent in parents:
                if start <= parent <= stop:
                    range_parents.append(parent) # Make sure only range is viewed ... could fail in unexpected ways
            queue[child_id] = range_parents

    while len(queue) > 0:
        queue, tasks_to_run = getNoDependencyTasks(queue)

        commands = []

        for task_id in tasks_to_run:
            task_name = tasks[task_id]['task_name']
            task_body = tasks[task_id]['task_body']

            msg("Executing task #%d: '%s'" % (task_id, task_name))

            os.chmod(path, 0755)    # make executable.
            env = dict(os.environ)
            env['workdir'] = cmd_args.workdir
            commands.append((path, env))

            # Remove from queue
            queue = deleteInQueue(queue, task_id)

        processes = [Popen(cmd[0], env=cmd[1]) for cmd in commands]
        # Wait for all to finish ... Naive and dumb, but want to verify this first before moving on
        for p in processes:
            p.wait()

def run_sge(tasks, start, stop):
    for task_id in sorted(tasks):
        if start <= task_id <= stop:
            cmd = ['qsub']
            parents = tasks[task_id]['task_parents']
            parents = [tasks[p]['jobid'] for p in parents if start <= p <= stop]
            if parents:
                cmd.extend(['-hold_jid', ','.join(map(str, parents))])
            cmd.extend(['-q', cmd_args.qsub])
            cmd.extend(['-N', "s{}.{}.wd={}".format(task_id, tasks[task_id]['task_name'], cmd_args.workdir)])
            cmd.extend(['-v', 'workdir='+cmd_args.workdir])
            cmd.append(tasks[task_id]['task_path'])
            msg("running: " + ' '.join(cmd))
            output = subprocess.check_output(cmd)
            msg("qsub output: " + output.strip())
            m = re.match(r"^\s*Your job (\d+)", output)
            if m:
                tasks[task_id]['jobid'] = m.group(1)
            else:
                msg("ERROR: couldn't get job id")
                sys.exit(1)

def executeTasks(cmd_args, tasks):

    try:
        start, stop = cmd_args.stages.split('-', 1)
    except ValueError:
        start = stop = cmd_args.stages

    if start == "": start = min(t for t in tasks if t > 0)
    if stop == "": stop = max(tasks)

    try:
        start = lookupTask(start, tasks)
        stop = lookupTask(stop, tasks)
    except ValueError as e:
        msg(e)
        sys.exit(1)

    if start == 0:
        msg("task 0 cannot be run by itself")
        sys.exit(1)
    if stop < start:
        msg("invalid range of tasks")
        sys.exit(1)


    if 0 in tasks:
        prologue_body = tasks[0]['task_body']
    else:
        prologue_body = []

    for task_id in tasks:
        if start <= task_id <= stop:
            task_name = tasks[task_id]['task_name']
            task_body = tasks[task_id]['task_body']
            path = write_body(cmd_args, prologue_body, task_id, task_name, task_body)
            tasks[task_id]['task_path'] = path

    if cmd_args.qsub is not None:
        run_sge(tasks, start, stop)
    else:
        run_local(tasks, start, stop)

def make_dir(input_dir):
    if not os.path.exists(input_dir):
        msg("Creating directory " + input_dir)
        os.makedirs(input_dir)


if __name__ == '__main__':
    cmd_args = parseArgs()
    tasks = parseTasks(cmd_args)
    make_dir(cmd_args.workdir)


    if cmd_args.list:
        # output the list of tasks.
        for task_id, task_params in tasks.items():
            msg("stage %d: %s" % (task_id, task_params['task_name']))
    else:
        executeTasks(cmd_args, tasks)
