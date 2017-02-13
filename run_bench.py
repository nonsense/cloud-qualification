#!/usr/bin/env python

import argparse
import boto3
import botocore
import yaml
import socket
import csv
import re
import sys
import time
import subprocess
from multiprocessing.pool import ThreadPool
#from googleapiclient import discovery
from threading import Thread
from pprint import pprint
#from oauth2client.client import GoogleCredentials
#credentials = GoogleCredentials.get_application_default()
#compute = discovery.build('compute','v1',credentials=credentials)
#service = discovery.build('deploymentmanager','v2',credentials=credentials)

args = None
data = {}
DATA_FILES="data"
asd_version = ""
user="ec2-user"
pool = ThreadPool(processes=2)
private_ips = None


try:
  import boto3
except:
  print "Boto3 is not installed. Please install boto3: sudo pip install boto3"
  exit


def parse_args():
    global args
    parser = argparse.ArgumentParser()
    
    parser.add_argument("-v"
                        , "--verbose"
                        , action="store_true"
                        , dest="debug"
                        , help="Enable verbose logging")

    parser.add_argument("-c"
                        , "--config"
                        , dest="config"
                        , required=True
                        , help="The config file to use")
    parser.add_argument("-n"
                        , "--namespace"
                        , dest="namespace"
                        , required=True
                        , help="The namespace to bench against")
    parser.add_argument("-d"
                        , "--deployment"
                        , dest="deployment"
                        , default='aerospike-test'
                        , nargs='?'
                        , help="The GCE deployment")
    parser.add_argument("-p"
                        , "--project"
                        , dest="project"
                        , default='aerospike-dev'
                        , nargs='?'
                        , help="The GCE project")
    parser.add_argument("-t"
                        , "--template"
                        , dest="template"
                        , default='cft/aerospike.json'
                        , nargs='?'
                        , help="The CFT for aerospike server")
    parser.add_argument("-o"
                        , "--ops"
                        , dest="ops"
                        , default=50000
                        , nargs='?'
                        , help="The target ops/s for YCSB")
    parser.add_argument("-z"
                        , "--threads"
                        , dest="threads"
                        , default=10
                        , nargs='?'
                        , help="The thread count for YCSB")
    parser.add_argument("-l"
                        , "--load"
                        , dest="load"
                        , action='store_true'
                        , help="Run only the Loading phase (Inserts)")
    parser.add_argument("-r"
                        , "--run"
                        , dest="run"
                        , action='store_true'
                        , help="Run only the Running phase (Read/Update)")

    args = parser.parse_args()
    
   
#----
# Compile YCSB
# Not actually needed
#---
def compileYCSB(ip, config):
    print "Compiling YCSB"
    ssh_command(ip,config, "source %s; cd YCSB; mvn -pl com.yahoo.ycsb:aerospike-binding -am clean package"%(profile))
    
#----
# Write a data file
#----
def write_datafile(filename,yaml_data):
    with open(DATA_FILES+'/'+filename, 'w') as outfile:
        yaml.dump(yaml_data,outfile, default_flow_style=True)

def add_data(metric, param, yaml_data,iteration):
    global data
    if iteration not in data:
        data[iteration] = {}
    key = "%s-%d"%(metric,param)
    if key not in data[iteration]:
        data[iteration][key] = yaml_data

def write_yaml_to_csv(yaml_data,config,version):
    filename = "%s_%s_%s.csv"%(config['Servers']['InstanceType'], config['Servers']['NumberOfInstances'], version)
    f = open(DATA_FILES+"/"+filename, 'wt')
    try:
        writer = csv.writer(f)
        i = 0
        for thread in yaml_data:
            for metric in sorted(yaml_data[thread]):
                param,value = re.split('-(?=\d*$)', metric)
                data_line = (value, )
                header_line = (param, )
                for operation in yaml_data[thread][metric]:
                    for stat in yaml_data[thread][metric][operation]:   
                        data_line += (yaml_data[thread][metric][operation][stat],)
                        header_line += (operation+' '+stat,) 
                if i %4 == 0:
                    writer.writerow(header_line)
                writer.writerow(data_line)   
                i+=1
    finally:
        f.close()


#-------
# Have client run a benchmark
#-------
def load_benchmark((ip,server,config)):
    print "Running insert benchmark"
    stdout= ssh_command(ip,config,"source %s; cd YCSB; bin/ycsb load aerospike -s -threads %s -target %s -P workloads/workload-aerospike -p as.host=%s -p as.namespace=%s 2> load.log"%(profile,args.threads,args.ops,server,args.namespace))
    if args.debug:
        pprint(stdout)
    return stdout

def run_benchmark((ip,server,config)):
    print "Running read/update benchmark"
    stdout= ssh_command(ip,config, "source %s; cd YCSB; bin/ycsb run aerospike -s -threads %s -target %s -P workloads/workload-aerospike -p as.host=%s -p as.namespace=%s 2> run.log"%(profile,args.threads,args.ops,server,args.namespace))
    if args.debug:
        pprint(stdout)
    return stdout

    
def parse_output(ycsb_output):
    ycsb_dict = {}
    for line in ycsb_output.split("\r\n"):
        if line == '':
            continue
        pprint(line)
        items = line.split(',')
        category = items[0].strip('[]')
        if category not in ycsb_dict:
            ycsb_dict[category] = {}
        ycsb_dict[category][items[1]] = items[2]
    return ycsb_dict

#--------
# Run the tests found in the Tests section of the config
#-------
def run_tests(client_ips,server,config,iteration):
    global asd_version, private_ips  
    asd_version  = get_asd_version(server[0],config).replace(' ','_').strip()
    for test,values in config['Tests'].iteritems():
        start, end, interval = map(int, values.split(','))
        while (start <= end):
            params = []
            for ip in client_ips:
              params.append((ip,private_ips[0],config))
            if  not args.run or (args.run and args.load):
                stop_server(server,config)
                reset_server_ssd(server,config)
                if "write-block-size" == test:
                    update_server_config(server,test,"%dk"%start,config)
                else:
                    update_server_config(server,test,start,config)
                restart_server(server,config)
                results = pool.map(load_benchmark,params)
                i=0
                for result in results:
                  add_data(test+"_load",start,parse_output(result),i)
                  i+=1
#            pprint(data)
            if not args.load or (args.run and args.load):
                run_results = pool.map(run_benchmark,params)
                i=0
                for result in run_results:
                    add_data(test,start,parse_output(result),i)
                    i+=1

            if "write-block-size" == test:
                start *= interval
            else:
                start += interval

#---
# SSH command
#---
def ssh_command(ip,config,command):
    ssh_cmd = "ssh"
    if not args.debug:
        ssh_cmd += " -o LogLevel=QUIET "
    ssh_cmd += " -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -t -i "+config['PKey']+" "+user+"@"+ip
    sys_cmd = ssh_cmd +' "'+ command+'"'
    proc = subprocess.Popen(sys_cmd, shell=True, stdout=subprocess.PIPE)
    sys.stdout.write("\n")
    return proc.stdout.read()

#----
# Reconfigure the server
#-----
def get_asd_version(server,config):
    print "Obtaining Server Version"
    return ssh_command(server,config, "asd --version")

def reset_config(servers,config):
    print "Resetting config to default"
    for server in servers:
      ssh_command(server,config, "sudo cp /etc/aerospike/aerospike.bak /etc/aerospike/aerospike.conf")

def backup_config(servers,config):
    print "Backing up default config"
    for server in servers:
      ssh_command(server,config, "sudo cp /etc/aerospike/aerospike.conf /etc/aerospike/aerospike.bak")

def update_server_config(servers, metric, value, config):
    print "Reconfiguring server metric: %s with value %s"%(metric, value)
    command = "sed 's/%s.*/%s %s/' /etc/aerospike/aerospike.orig | sudo tee /etc/aerospike/aerospike.conf"%(metric,metric,value)
    if not args.debug:
        command += " > /dev/null"
    for server in servers:
      ssh_command( server, config, command)

def reset_server_ssd(servers, config):
    print "Wiping SSD at /dev/sd[bcde]"
    for server in servers:
      ssh_command(server,config, "sudo dd if=/dev/zero bs=1M count=1 of=/dev/sdb")
      if config['Servers']['InstanceType'] in ["c3.large","c3.xlarge","c3.2xlarge","c3.4xlarge","c3.8xlarge","m3.xlarge","m3.2xlarge","r3.8xlarge","i2.2xlarge"]:
          ssh_command(server,config, "sudo dd if=/dev/zero bs=1M count=1 of=/dev/sdc")
      if config['Servers']['InstanceType'] in ["i2.2xlarge"]:
          ssh_command(server,config, "sudo dd if=/dev/zero bs=1M count=1 of=/dev/sdd")
          ssh_command(server,config, "sudo dd if=/dev/zero bs=1M count=1 of=/dev/sde")

def stop_server(servers,config):
    print "Stopping Aerospike"
    for server in servers:
      ssh_command(server,config, "sudo service aerospike stop")

def restart_server(servers,config):
    print "Restarting Aerospike"
    for server in servers:
      ssh_command(server,config, "sudo service aerospike restart")
    print "Waiting for clustering"
    time.sleep(60)
    for server in servers:
      print "Checking for migrations on %s"%(server)
      while is_migrating(server):
        time.sleep(20)
        print '.',
        sys.stdout.flush()
    print "Migrations finished"


def is_migrating(server):
    response = ""
    migrate_progress_send = 0
    migrate_progress_recv = 0
    partitions_remaining = 0
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((server,3003))
    except:
        return True
    s.send("statistics\n")
    response = readall(s)
#    print response
    s.close()
    if response:
        pairs = response.split(';')
        for pair in pairs:
            try:
                k, v = pair.split('=', 2)
                if k == 'migrate_progress_send':
                    migrate_progress_send = int(v)
                if k == 'migrate_progress_recv':
                    migrate_progress_recv = int(v)
                if k == 'migrate_partitions_remaining':
                    partitions_remaining = int(v)
            except:
                continue
    else:
        print "No response received"
#    print "migrate_progress_send %s"%migrate_progress_send
#    print "migrate_progress_recv %s"%migrate_progress_recv
#    print "paritions_remaining %s"%partitions_remaining
    if migrate_progress_send == 0 and migrate_progress_recv == 0 and partitions_remaining == 0:
        return False
    else:
        return True

def readall(s):
    chunks = []
    while True:
        chunk = s.recv(1024 * 64)
        if not chunk:
            break
        chunks.append(chunk)
        if chunk[len(chunk)-1] == '\n':
            break
    if len(chunks) > 0:
        return ''.join(chunks)
    return None


#   EEEE CCCC 2222
#   E    C       2
#   EEE  C    2222
#   E    C    2
#   EEEE CCCC 2222

#-----
# Extract AutoScalingGroup from CF stack
#-----
def extract_autoscaling_group(cf_client,stack_id):
    stack = cf_client.list_stack_resources(StackName=stack_id)['StackResourceSummaries']
    for resource in stack:
        if resource['ResourceType'] == 'AWS::AutoScaling::AutoScalingGroup':
            return resource['PhysicalResourceId']

#----
# Extract instance Ids from AutoScalingGroup
#----

def extract_instance_ids(autoscaling_client,autoscaling_group):
    d = autoscaling_client.describe_auto_scaling_groups(AutoScalingGroupNames=[autoscaling_group])
    return d['AutoScalingGroups'][0]['Instances']


#-----
# Extract instance IP from instance list
#----

def extract_instance_ip(ec2_client,instances,public=True):
  ips = []
  for instance in instances:
    d = ec2_client.Instance(instance['InstanceId'])
    if public:
        ips.append(d.public_ip_address)
    else:
        ips.append(d.private_ip_address)
  return ips


#   GGG  CCC EEEE
#  G    C    E
#  G GG C    EEE
#  G  G C    E
#   GGG  CCC EEEE

#------
# Get server IP
#------
def extract_gce_ips(project,zone,instances):
    publicIP=[]
    for name in instances:
        result = compute.instances().get(instance=name,zone=zone,project=project).execute()
        publicIP.append(result['networkInterfaces'][0]['accessConfigs'][0]['natIP'])
    return publicIP

def extract_gce_instances(project,deployment):
    instances=[]
    result = service.resources().list(project=project,deployment=deployment).execute()
    for resource in result['resources']:
        if 'compute.v1.instance' == resource['type']:
            instances.append(resource['name'])
    return instances

#-----
# The parallelized workload
#----
def run_main(iteration):
    global private_ips
    if "EC2" == config['Platform']:
        print "Connecting to Cloudformation at %s"%(config['Region'])
        client = boto3.client('cloudformation',region_name=config['Region'])
        autoscale_client = boto3.client('autoscaling',region_name=config['Region'])
        ec2_resource = boto3.resource('ec2',region_name=config['Region'])
        group = extract_autoscaling_group(client,config['DCNames'])
        client_group = extract_autoscaling_group(client,config['DCNames']+'-clients')
    if "GCP" == config['Platform']:
        group = extract_gce_instances(args.project,args.deployment)
    if args.debug:
        pprint(group)
    if group is None:
        print "Instances not found. Aborting"
        exit(1)
    if "EC2" == config['Platform']:
        instances = extract_instance_ids(autoscale_client,group)
        client_instances = extract_instance_ids(autoscale_client,client_group)
        server_ips = extract_instance_ip(ec2_resource,instances,public=True)
        client_ips = extract_instance_ip(ec2_resource,client_instances)
        private_ips = extract_instance_ip(ec2_resource,instances,public=False)
    if "GCP" == config['Platform']:
        server_ips = extract_gce_ips(args.project,"us-central1-f",group)
        client_ips = extract_gce_ips(args.project,"us-central1-f",["bench-client"])   
    
    run_tests(client_ips,server_ips,config, iteration)

##################
# begin main part
##################

parse_args()

if args.debug:
    print "Reading params.yml file"

# Read in the params file
with open(args.config,'r') as stream:
    try:
        config = yaml.load(stream)
    except yaml.YAMLError as e:
        print(e)
        exit(1)

profile=".bash_profile"    # GCP: .profile, EC2: .bash_profile
if "GCP" ==  config['Platform']:
    profile=".profile"

if args.debug:
    pprint(config)

run_main(0)

write_datafile("Results_%s_%s_%s.yaml"%(config['Servers']['NumberOfInstances'],config['Servers']['InstanceType'],asd_version),data)
write_yaml_to_csv(data,config,asd_version)

