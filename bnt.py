#!/usr/bin/env python3

import sys
from blockchain_parser.blockchain import Blockchain
from operator import attrgetter

import sqlite3

conn = None
c = None
def open_db():
    global conn
    global c
    conn = sqlite3.connect('transactions.sqlite3')
    #conn = sqlite3.connect(':memory:')
    c = conn.cursor()
open_db()

def query_execute(query, args=(), explain=False):
    global c
    if explain:
        explain_query = 'EXPLAIN QUERY PLAN ' + query
        c.execute(explain_query, args)
        print("explanation of " + query)
        print(c.fetchone())
    c.execute(query, args)

query_execute('''SELECT name FROM sqlite_master WHERE type='table' AND name='unspent_outputs';''')
result = c.fetchone()
if result == None:
    
    print("Creating unspent_outputs table...")
    query_execute('''CREATE TABLE unspent_outputs
                 (tx_hash text, output_number int, address text, amount int, multi_address int)''')
    query_execute('CREATE UNIQUE INDEX tx_output ON unspent_outputs (tx_hash, output_number);')

    print("Creating balances table...")
    query_execute('''CREATE TABLE balances
                 (address text, amount int)''')
    query_execute('CREATE UNIQUE INDEX point ON balances (address);')

def commit_db_and_exit():
    conn.commit()
    conn.close()
    exit()

import signal
def signal_handler(signal, frame):
        print('\nReceived SIGINT, commiting DB and quitting...')
        commit_db_and_exit()

signal.signal(signal.SIGINT, signal_handler)



insertions_since_last_commit = 0
total_outputs_inserted = 0

def process_transfer(destination_address, value, tx_hash, output_number, multi_address = False):
    global insertions_since_last_commit
    global total_outputs_inserted

    address = destination_address

    row = (tx_hash, output_number, address, value, multi_address)
    try:
        query_execute('INSERT INTO unspent_outputs VALUES(?, ?, ?, ?, ?)', row)
    except sqlite3.IntegrityError as e:
        print(e)
        print("WARNING: ignoring transaction with duplicate hash. should be extremely rare. see https://bitcoin.stackexchange.com/questions/11999/can-the-outputs-of-transactions-with-duplicate-hashes-be-spent")
        print(row)
    total_outputs_inserted = total_outputs_inserted + 1

    # update balance of address
    query_execute('SELECT * FROM balances WHERE address = ?', (address,), explain=True)
    result = c.fetchone()
    # print(result)
    new_amount = 0
    if result != None:
        # print("Found previous balance %s" % (result,))
        new_amount = result[1] + value

        # if result[0] == timestamp.isoformat():
        query_execute('UPDATE balances SET amount = ? WHERE address = ?', (new_amount, address), explain=True)
    else:
        try:
            query_execute('INSERT INTO balances VALUES(?, ?)', (address, new_amount))
        except sqlite3.IntegrityError as e:
            print(e)
            print("while trying to insert %s" % ((address, new_amount),))
            commit_db_and_exit()

    insertions_since_last_commit = insertions_since_last_commit + 1
    if insertions_since_last_commit > 10000:
        conn.commit()
        # will closing and re-opening plug a memory leak?
        # conn.close()
        # open_db()
        # print("Closed and re-opened db")
        insertions_since_last_commit = 0

# some outputs of value have zero addresses.
# will credit them to unique "dev null" pseudo-addresses.
def make_devnull_address_generator():
    n = 0
    while True:
        yield "devnull_" + str(n).zfill(10)
        n = n + 1
devnull_address = make_devnull_address_generator()

def process_output(timestamp, tx_hash, output_number, _output):

    global devnull_address

    # don't know how to handle outputs mentioning more than 1 address at the moment
    if len(_output.addresses) == 1:
        process_transfer(_output.addresses[0].address, _output.value, tx_hash, output_number)

    elif len(_output.addresses) == 0:
        if _output.value != 0:
            print("Info: 0 address output. tx=%s output_number=%s output=%s" % (tx_hash, output_number, _output))
            print("...of value = %s satoshis" % (_output.value))
            pseudo_address = next(devnull_address)
            print("...crediting to " + pseudo_address + " LOL")
            process_transfer(pseudo_address, _output.value, tx_hash, output_number)
            # print("...quitting, we should handle this right?")
            # commit_db_and_exit()
    else:
        print("multi address output. depositing to all addresses and flagging outputs as multi address")
        print(_output.addresses)
        print("Value:")
        print(_output.value)
        print("Type:")
        print(_output.type)

        for output_address in _output.addresses:
            process_transfer(output_address.address, _output.value, tx_hash, output_number, multi_address=True)

        # print("don't know how to handle that, quitting")
        # commit_db_and_exit()



def process_input(_input):
    # print(_input)
    query_execute('SELECT address, amount FROM unspent_outputs WHERE tx_hash=? AND output_number=?', (_input.transaction_hash, _input.transaction_index), explain=True)
    result = c.fetchone()
    # print(result)
    if result != None:
        # print("Found %s" % (result,))
        # an output can only be spent through an input once! we can remove this row!
        query_execute('DELETE FROM unspent_outputs WHERE tx_hash=? AND output_number=?', (_input.transaction_hash, _input.transaction_index), explain=True)

def get_number_of_unspent_outputs():
    query_execute('SELECT COUNT(*) FROM unspent_outputs', explain=True)
    result = c.fetchone()[0]
    # print(result)
    return result

# Instantiate the Blockchain by giving the path to the directory 
# containing the .blk files created by bitcoind
tx_index = 0
print("Initializing blockchain at " + sys.argv[1])
blockchain = Blockchain(sys.argv[1])
print("Initialized. Loading blocks...")
unordered_blocks = []
for b, block in enumerate(blockchain.get_unordered_blocks()):
    unordered_blocks.append(block)
    if b % 1000 == 0:
        print("Loaded " + str(b) + " blocks", end="\r")
total_blocks = len(unordered_blocks)
print("Loaded all " + str(total_outputs_inserted) + " blocks. Sorting by time...")
blocks = sorted(unordered_blocks, key=attrgetter('header.timestamp'))
unordered_blocks = None
print("Sorted.")

tx_count = 0
output_count = 0

block_number = 0

# from pympler.tracker import SummaryTracker
# tracker = SummaryTracker()

while len(blocks) > 0:
    block = blocks.pop(0)
# for block_number, block in enumerate(blocks):
    # print("block %s / %s: %s" % (block_number, len(blocks), block.header.timestamp.isoformat(),))

    print(block.header.timestamp.isoformat())

    for tx in block.transactions:

        tx_count = tx_count + 1

        # example input referencing a previous output
        #
        # tx=66f701374a05702aa1ab920486cfc1b7a1f643b6ed75c04f2583d412f12ff88b input_num=0 input=Input(06b617171d45dccf383bf82a0cf5cd5a142d6a9bf91393223d46f041bf0380b4,78)
        # tx=66f701374a05702aa1ab920486cfc1b7a1f643b6ed75c04f2583d412f12ff88b outputno=0 type=[Address(addr=144vPbnRe5ETJiRLqbVtEUqtgwAAb1hJLw)] value=316327
        # tx=66f701374a05702aa1ab920486cfc1b7a1f643b6ed75c04f2583d412f12ff88b outputno=1 type=[Address(addr=16tBxGUb95wa7yhkAn3uSU125ZxWzqFK8g)] value=3673759
        # tx=66f701374a05702aa1ab920486cfc1b7a1f643b6ed75c04f2583d412f12ff88b outputno=2 type=[Address(addr=1DiUJ9PXnump3KHsjc1qsp3E3LUbPvAxR4)] value=7574557
        # tx=635745100dc559787dc3b09bd31a62155084dc582aff741debb18828b91a8ec0 input_num=0 input=Input(66f701374a05702aa1ab920486cfc1b7a1f643b6ed75c04f2583d412f12ff88b,2)

        for no, _input in enumerate(tx.inputs):
            input_address = process_input(_input)
            # print("tx=%s input_num=%s input=%s" % (tx.hash, no, _input))

        # so, 1 or more outputs may be change wallets, wallets "secretly"
        # created by the wallet client to receive change, since only entire previous
        # outputs can be inputs.
        # meaning it's not straightforward to maintain a balance for every "human"
        # wallet, in fact hierarchichal deterministic wallets like Jaxx skip to a new
        # real wallet address with every transaction "as a way to make your balance" private
        # but can we not, say, when a transaction has multiple inputs, assume
        # with reasonable confidence that they all belong to the same human?
        # that would still only let us associate some change/privacy wallets with
        # each other, and maybe wallet clients do lots of stuff to retain privacy
        # in the long run, e.g. even after everything is spent
        
        for no, output in enumerate(tx.outputs):
            output_count = output_count + 1
            if tx.hash == "d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599":
                print("processing tx d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599 output #" + str(no))
            process_output(block.header.timestamp, tx.hash, no, output)
            # print("tx=%s outputno=%d type=%s value=%s" % (tx.hash, no, output.addresses, output.value))
        
        tx_index = tx_index + 1
        if tx_index % 1000 == 0:
            percent = round(100 * block_number / total_blocks)
            print("%s%% done. unspent outputs %s / %s total outputs" % (percent, get_number_of_unspent_outputs(), total_outputs_inserted), end="\r")
        
        print("--------------------------------------------\n\n\n")
            
    # if(block_number % 1000 == 0):
    #     print("Finished block " + str(block_number))
    #     tracker.print_diff()

    block_number = block_number + 1

print("Processed %s transactions and %s outputs." % (tx_count, output_count))
