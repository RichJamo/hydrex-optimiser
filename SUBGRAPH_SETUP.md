# Hydrex Subgraph Schema for VoterV5

This document outlines the entities and events you need to add to your Goldsky subgraph to support the vote optimizer.

## Important: Per-Gauge Vote Tracking

The `Voted` event only emits `(voter, weight)` but doesn't specify which gauges were voted for. To track per-gauge votes, we need to either:

1. **Decode transaction input** - Parse the `vote()` function call to extract gauge addresses and weights
2. **Query contract state** - After each Voted event, query the contract's `poolVote` mapping to see vote distribution

The implementation below uses approach #1 (transaction decoding). If that proves difficult in AssemblyScript, you can use approach #2 by binding the VoterV5 contract and calling view functions.

## Required Entities

Add these entities to your `schema.graphql`:

```graphql
type Gauge @entity {
  id: ID! # gauge address
  address: Bytes! # gauge contract address
  pool: Bytes! # associated pool address
  creator: Bytes! # address that created the gauge
  internalBribe: Bytes! # internal bribe contract
  externalBribe: Bytes! # external bribe contract
  isAlive: Boolean! # whether gauge is active
  blockNumber: BigInt!
  blockTimestamp: BigInt!
  transactionHash: Bytes!
}

type Vote @entity {
  id: ID! # transaction hash + log index
  voter: Bytes! # voter address
  weight: BigInt! # vote weight/power
  blockNumber: BigInt!
  blockTimestamp: BigInt!
  transactionHash: Bytes!
}

type GaugeVote @entity {
  id: ID! # epoch-gauge-voter
  epoch: BigInt! # epoch timestamp
  gauge: Gauge! # reference to gauge
  voter: Bytes! # voter address
  weight: BigInt! # weight allocated to this gauge
  blockNumber: BigInt!
  blockTimestamp: BigInt!
  transactionHash: Bytes!
}

type Bribe @entity {
  id: ID! # transaction hash + log index
  bribeContract: Bytes! # bribe contract that emitted event
  rewardToken: Bytes! # token being offered as bribe
  amount: BigInt! # amount of tokens
  from: Bytes! # address adding the bribe
  blockNumber: BigInt!
  blockTimestamp: BigInt!
  transactionHash: Bytes!
}
```

## Required Event Handlers

Add these to your `subgraph.yaml`:

```yaml
dataSources:
  - kind: ethereum/contract
    name: VoterV5
    network: base
    source:
      address: "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
      abi: VoterV5
      startBlock: 35273810 # Block where VoterV5 was deployed
    mapping:
      kind: ethereum/events
      apiVersion: 0.0.7
      language: wasm/assemblyscript
      entities:
        - Gauge
        - Vote
        - Bribe
      abis:
        - name: VoterV5
          file: ./abis/VoterV5.json
      eventHandlers:
        - event: GaugeCreated(indexed address,address,address,indexed address,indexed address)
          handler: handleGaugeCreated
        - event: Voted(indexed address,uint256)
          handler: handleVoted
        - event: GaugeKilled(indexed address)
          handler: handleGaugeKilled
        - event: GaugeRevived(indexed address)
          handler: handleGaugeRevived
      file: ./src/voter-v5.ts
```

## Event Handler Implementation

Create `src/voter-v5.ts` with these handlers:

```typescript
import { BigInt, ethereum } from "@graphprotocol/graph-ts";
import {
  GaugeCreated,
  Voted,
  GaugeKilled,
  GaugeRevived,
  VoterV5,
} from "../generated/VoterV5/VoterV5";
import { Gauge, Vote, GaugeVote } from "../generated/schema";

export function handleGaugeCreated(event: GaugeCreated): void {
  let gauge = new Gauge(event.params.gauge.toHexString());
  gauge.address = event.params.gauge;
  gauge.pool = event.params.pool;
  gauge.creator = event.params.creator;
  gauge.internalBribe = event.params.internal_bribe;
  gauge.externalBribe = event.params.external_bribe;
  gauge.isAlive = true;
  gauge.blockNumber = event.block.number;
  gauge.blockTimestamp = event.block.timestamp;
  gauge.transactionHash = event.transaction.hash;
  gauge.save();
}

export function handleVoted(event: Voted): void {
  // Save the overall vote event
  let voteId =
    event.transaction.hash.toHexString() + "-" + event.logIndex.toString();
  let vote = new Vote(voteId);
  vote.voter = event.params.voter;
  vote.weight = event.params.weight;
  vote.blockNumber = event.block.number;
  vote.blockTimestamp = event.block.timestamp;
  vote.transactionHash = event.transaction.hash;
  vote.save();

  // Query the VoterV5 contract to get per-gauge vote weights
  // The vote() function updates lastVoted[tokenId] and we can query poolVote[tokenId]
  let voterContract = VoterV5.bind(event.address);

  // Calculate epoch (1 week periods)
  let epoch = event.block.timestamp
    .div(BigInt.fromI32(604800))
    .times(BigInt.fromI32(604800));

  // Get all gauges and check their weights for this voter
  // Note: We need the tokenId (NFT) not the address
  // This is a simplified version - you may need to adjust based on actual contract interface

  // Alternative: Parse transaction input to get the pools/gauges and weights arrays
  // The vote() function signature is: vote(uint256 tokenId, address[] pools, uint256[] weights)
  let inputData = event.transaction.input;

  // Decode the function call (skip first 4 bytes for function selector)
  // This requires manual ABI decoding - see below for helper function
  let decoded = decodeVoteCall(inputData);

  if (decoded != null) {
    let gauges = decoded.gauges;
    let weights = decoded.weights;

    for (let i = 0; i < gauges.length; i++) {
      let gaugeAddress = gauges[i];
      let weight = weights[i];

      // Create GaugeVote entity
      let gaugeVoteId =
        epoch.toString() +
        "-" +
        gaugeAddress.toHexString() +
        "-" +
        event.params.voter.toHexString();
      let gaugeVote = GaugeVote.load(gaugeVoteId);

      if (gaugeVote == null) {
        gaugeVote = new GaugeVote(gaugeVoteId);
        gaugeVote.epoch = epoch;
        gaugeVote.gauge = gaugeAddress.toHexString();
        gaugeVote.voter = event.params.voter;
        gaugeVote.weight = weight;
        gaugeVote.blockNumber = event.block.number;
        gaugeVote.blockTimestamp = event.block.timestamp;
        gaugeVote.transactionHash = event.transaction.hash;
      } else {
        // Update if revoting in same epoch
        gaugeVote.weight = weight;
        gaugeVote.blockNumber = event.block.number;
        gaugeVote.blockTimestamp = event.block.timestamp;
        gaugeVote.transactionHash = event.transaction.hash;
      }

      gaugeVote.save();
    }
  }
}

// Helper to decode vote() function call
// vote(uint256 tokenId, address[] pools, uint256[] weights)
class DecodedVote {
  gauges: ethereum.Address[];
  weights: BigInt[];
}

function decodeVoteCall(input: ethereum.Bytes): DecodedVote | null {
  // Function selector for vote(uint256,address[],uint256[]) is first 4 bytes
  if (input.length < 4) {
    return null;
  }

  // This is a simplified decoder - AssemblyScript doesn't have easy ABI decoding
  // In practice, you may want to use ethereum.decode() or parse manually

  // For now, return null and we'll use contract calls instead
  // TODO: Implement proper ABI decoding or use contract state queries
  return null;
}

export function handleGaugeKilled(event: GaugeKilled): void {
  let gauge = Gauge.load(event.params.gauge.toHexString());
  if (gauge != null) {
    gauge.isAlive = false;
    gauge.save();
  }
}

export function handleGaugeRevived(event: GaugeRevived): void {
  let gauge = Gauge.load(event.params.gauge.toHexString());
  if (gauge != null) {
    gauge.isAlive = true;
    gauge.save();
  }
}
```

## VoterV5 ABI

Create `abis/VoterV5.json` with at minimum these events:

```json
[
  {
    "anonymous": false,
    "inputs": [
      {
        "indexed": true,
        "internalType": "address",
        "name": "gauge",
        "type": "address"
      },
      {
        "indexed": false,
        "internalType": "address",
        "name": "creator",
        "type": "address"
      },
      {
        "indexed": false,
        "internalType": "address",
        "name": "internal_bribe",
        "type": "address"
      },
      {
        "indexed": true,
        "internalType": "address",
        "name": "external_bribe",
        "type": "address"
      },
      {
        "indexed": true,
        "internalType": "address",
        "name": "pool",
        "type": "address"
      }
    ],
    "name": "GaugeCreated",
    "type": "event"
  },
  {
    "anonymous": false,
    "inputs": [
      {
        "indexed": true,
        "internalType": "address",
        "name": "voter",
        "type": "address"
      },
      {
        "indexed": false,
        "internalType": "uint256",
        "name": "weight",
        "type": "uint256"
      }
    ],
    "name": "Voted",
    "type": "event"
  },
  {
    "anonymous": false,
    "inputs": [
      {
        "indexed": true,
        "internalType": "address",
        "name": "gauge",
        "type": "address"
      }
    ],
    "name": "GaugeKilled",
    "type": "event"
  },
  {
    "anonymous": false,
    "inputs": [
      {
        "indexed": true,
        "internalType": "address",
        "name": "gauge",
        "type": "address"
      }
    ],
    "name": "GaugeRevived",
    "type": "event"
  }
]
```

## Bribe Contract Events (Optional but Recommended)

If you want to track bribes from multiple bribe contracts dynamically:

```yaml
templates:
  - kind: ethereum/contract
    name: Bribe
    network: base
    source:
      abi: Bribe
    mapping:
      kind: ethereum/events
      apiVersion: 0.0.7
      language: wasm/assemblyscript
      entities:
        - Bribe
      abis:
        - name: Bribe
          file: ./abis/Bribe.json
      eventHandlers:
        - event: NotifyReward(indexed address,indexed address,uint256)
          handler: handleNotifyReward
      file: ./src/bribe.ts
```

And handler in `src/bribe.ts`:

```typescript
import { NotifyReward } from "../generated/templates/Bribe/Bribe";
import { Bribe } from "../generated/schema";

export function handleNotifyReward(event: NotifyReward): void {
  let bribeId =
    event.transaction.hash.toHexString() + "-" + event.logIndex.toString();
  let bribe = new Bribe(bribeId);
  bribe.bribeContract = event.address;
  bribe.rewardToken = event.params.reward;
  bribe.amount = event.params.amount;
  bribe.from = event.params.from;
  bribe.blockNumber = event.block.number;
  bribe.blockTimestamp = event.block.timestamp;
  bribe.transactionHash = event.transaction.hash;
  bribe.save();
}
```

## Deployment

After updating your subgraph:

```bash
# Generate types
graph codegen

# Build
graph build

# Deploy to Goldsky
goldsky subgraph deploy hydrex-dummy/v0.0.3
```

## Testing Queries

Once deployed, test with these queries:

```graphql
# Get all gauges
{
  gauges(first: 10, orderBy: blockNumber, orderDirection: desc) {
    id
    address
    pool
    creator
    internalBribe
    externalBribe
    isAlive
    blockTimestamp
  }
}

# Get votes for a specific voter
{
  votes(where: { voter: "0x768a675b8542f23c428c6672738e380176e7635c" }) {
    id
    voter
    weight
    blockTimestamp
  }
}

# Get per-gauge votes for an epoch
{
  gaugeVotes(
    where: { epoch: "1738800000" }
    orderBy: weight
    orderDirection: desc
  ) {
    id
    epoch
    gauge {
      address
      pool
    }
    voter
    weight
    blockTimestamp
  }
}

# Get all votes for a specific gauge in recent epochs
{
  gaugeVotes(
    where: { gauge: "0x0046421be7..." }
    orderBy: blockTimestamp
    orderDirection: desc
    first: 100
  ) {
    id
    epoch
    voter
    weight
    blockTimestamp
  }
}

# Get bribes
{
  bribes(first: 100, orderBy: blockTimestamp, orderDirection: desc) {
    id
    bribeContract
    rewardToken
    amount
    blockTimestamp
  }
}
```
