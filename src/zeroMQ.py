#!/usr/bin/env python3

import time
import json
import binascii
import asyncio
import zmq
import zmq.asyncio
import signal
import math
import struct
import sys
import os
from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
from enum import Enum

SATS_PER_BTC = 100000000


class ZMQHandler():
    def __init__(self, logging, rocks, mempool_state):
        if ('RPC_USER' not in os.environ
            or 'RPC_PASSWORD' not in os.environ
            or 'RPC_HOST' not in os.environ
                or 'RPC_PORT' not in os.environ):
            raise Exception(
                'Need to specify RPC_USER and RPC_PASSWORD, RPC_HOST, RPC_PORT environs')

        if 'ZMQ_PORT' not in os.environ or 'ZMQ_HOST' not in os.environ:
            raise Exception('Need to specify ZMQ_PORT and ZMQ_HOST environs')

        self.rpc_connection = AuthServiceProxy("http://%s:%s@%s:%s" %
                                               (os.environ['RPC_USER'], os.environ['RPC_PASSWORD'],  os.environ['RPC_HOST'], os.environ['RPC_PORT']))
        self.loop = asyncio.get_event_loop()
        self.zmqContext = zmq.asyncio.Context()

        self.zmqSubSocket = self.zmqContext.socket(zmq.SUB)
        self.zmqSubSocket.setsockopt(zmq.RCVHWM, 0)
        self.zmqSubSocket.setsockopt_string(zmq.SUBSCRIBE, "hashblock")
        self.zmqSubSocket.setsockopt_string(zmq.SUBSCRIBE, "rawtx")
        self.zmqSubSocket.connect(
            "tcp://%s:%s" % (os.environ['ZMQ_HOST'], os.environ['ZMQ_PORT']))
        self.logging = logging
        self.rocks = rocks
        self.mempool_state = mempool_state

    def add_tx(self, serialized_tx):
        try:
            # Skip coin base tx
            if len(serialized_tx['vin']) == 1 and 'coinbase' in serialized_tx['vin'][0]:
                return
            # Decode tx id and save in rocks
            fees = self.getTransactionFees(serialized_tx)
            fee_rate = fees / serialized_tx['size']
            # Delete inputs and outputs to perserve space
            serialized_tx.pop('vin', None)
            serialized_tx.pop('vout', None)
            serialized_tx.pop('hex', None)
            # Concat tx obj with mempool state
            tx = (
                {**{
                    'feerate': float(fee_rate),
                    'fee': float(fees),
                    'mempooldate': int(time.time()),
                    'mempoolgrowthrate': self.mempool_state.mempool_growth_rate_service.growth_rate,
                    'networkdifficulty': self.mempool_state.network_difficulty.network_difficulty,
                    'averageconfirmationtime': self.mempool_state.average_confirmation_time_service.average_confirmation_time,
                    'mempoolsize': self.mempool_state.mempool_size_service.mempool_size,
                    # TODO  'feeRateBuckets': self.mempool_state.fee_bucket_service.fee_buckets,
                    'minerrevenue': self.mempool_state.miner_revenue_service.miner_revenue,
                    'totalhashrate': self.mempool_state.total_hash_rate_service.total_hash_rate,
                    'marketprice': self.mempool_state.market_price_service.market_price,
                    'dayofweek': self.mempool_state.date_service.day_of_week,
                    'hourofday': self.mempool_state.date_service.hour_of_day,
                    'monthofyear': self.mempool_state.date_service.month_of_year,
                    'averagemempoolfee': self.mempool_state.mempool_fee_service.average_fee,
                    'averagemempoolfeerate': self.mempool_state.mempool_fee_service.average_fee_rate,
                    'averagemempooltxsize': self.mempool_state.mempool_size_service.average_mempool_tx_size,
                    'recommendedfeerates': self.mempool_state.fee_service.rates
                }, **serialized_tx})

            self.logging.info(
                '[ZMQ]: persisting tx %s', tx)
            self.rocks.write_mempool_tx(tx)

        except Exception as e:
            self.logging.error(
                '[ZMQ]: Failed to decode and persist tx %s' % e)
            sys.exit(1)
            return

    def getInputValue(self, txid, vout):

        serialized_tx = self.rpc_connection.decoderawtransaction(
            self.rpc_connection.getrawtransaction(txid))
        output = next((d for (index, d) in enumerate(
            serialized_tx['vout']) if d["n"] == vout), None)
        return output['value']

    def getTransactionFees(self, tx):
        # Add up output values
        output_value = 0
        [output_value := output_value + vout['value'] for vout in tx['vout']]
        # Add up input values
        input_value = 0
        [input_value := input_value +
            self.getInputValue(vin['txid'], vin['vout']) for vin in tx['vin']]

        # Or equal case added, b/c some tx are not going to spend any funds
        # assert(input_value >= output_value)
        return float((input_value - output_value) * SATS_PER_BTC)

    async def handle(self):
        self.logging.info('[ZMQ]: Starting to handel zmq topics')
        topic, body, seq = await self.zmqSubSocket.recv_multipart()
        self.logging.info('[ZMQ]: Body %s %s' % (topic, seq))
        if topic == b"rawtx":
            # Tx entering mempool
            try:
                self.logging.info('[ZMQ]: Recieved Raw TX')
                serialized_tx = self.rpc_connection.decoderawtransaction(
                    binascii.hexlify(body).decode("utf-8"))
                # TODO use class cache for this check
                existing_tx = self.rocks.get_tx(serialized_tx['txid'])
                if existing_tx == None:
                    self.add_tx(serialized_tx)
                else:
                    existing_tx = json.loads(existing_tx)
                    self.logging.info(
                        '[ZMQ]: Updating conf time for %s' % serialized_tx['txid'])
                    conf_time = int(time.time())
                    time_to_conf_time = conf_time - existing_tx['mempooldate']
                    fee_rate = math.floor(existing_tx['feerate'])
                    self.mempool_state.conf_time_per_fee_rate_service.update_conf_times_per_fee(
                        fee_rate, time_to_conf_time)
                    self.rocks.update_tx_conf_time(
                        serialized_tx['txid'], conf_time, self.mempool_state.conf_time_per_fee_rate_service.conf_times_per_fee_bucket)

            except Exception as e:
                self.logging.info('[ZMQ]: Failed to write mempool entry')
                self.logging.info(e)

        asyncio.ensure_future(self.handle())

    def start(self):
        self.loop.create_task(self.handle())
        self.loop.run_forever()

    def stop(self):
        self.loop.stop()
        self.zmqContext.destroy()
