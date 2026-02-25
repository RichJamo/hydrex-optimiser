#!/usr/bin/env python3
import sqlite3
from dotenv import dotenv_values
from web3 import Web3

from data.fetchers.fetch_epoch_bribes import BRIBE_ABI
from config.settings import WEEK


def main() -> None:
    rpc = dotenv_values('.env').get('RPC_URL')
    w3 = Web3(Web3.HTTPProvider(rpc))
    conn = sqlite3.connect('data/db/data.db')
    cur = conn.cursor()

    epoch = 1758153600
    row = cur.execute(
        'SELECT boundary_block, vote_epoch FROM epoch_boundaries WHERE epoch=?',
        (epoch,),
    ).fetchone()
    if not row:
        print('No boundary row found')
        return

    boundary_block, vote_epoch = row
    print(f'epoch={epoch} vote_epoch={vote_epoch} boundary_block={boundary_block} delta={epoch-vote_epoch}')

    pairs = cur.execute(
        '''
        SELECT bribe_contract, reward_token
        FROM bribe_reward_tokens
        WHERE is_reward_token = 1
        LIMIT 80
        '''
    ).fetchall()

    success = 0
    errors = 0
    nonzero_vote = []
    nonzero_epoch = []
    nonzero_next = []

    for bribe_addr, token_addr in pairs:
        try:
            contract = w3.eth.contract(address=Web3.to_checksum_address(bribe_addr), abi=BRIBE_ABI)

            period_finish_1, rewards_1, last_update_1 = contract.functions.rewardData(
                Web3.to_checksum_address(token_addr),
                int(vote_epoch),
            ).call(block_identifier=int(boundary_block))

            period_finish_2, rewards_2, last_update_2 = contract.functions.rewardData(
                Web3.to_checksum_address(token_addr),
                int(epoch),
            ).call(block_identifier=int(boundary_block))

            period_finish_3, rewards_3, last_update_3 = contract.functions.rewardData(
                Web3.to_checksum_address(token_addr),
                int(vote_epoch + WEEK),
            ).call(block_identifier=int(boundary_block))

            success += 1

            if int(rewards_1) > 0 and len(nonzero_vote) < 5:
                nonzero_vote.append((bribe_addr, token_addr, int(rewards_1), int(period_finish_1), int(last_update_1)))
            if int(rewards_2) > 0 and len(nonzero_epoch) < 5:
                nonzero_epoch.append((bribe_addr, token_addr, int(rewards_2), int(period_finish_2), int(last_update_2)))
            if int(rewards_3) > 0 and len(nonzero_next) < 5:
                nonzero_next.append((bribe_addr, token_addr, int(rewards_3), int(period_finish_3), int(last_update_3)))

        except Exception:
            errors += 1

    print(f'success={success} errors={errors} sampled_pairs={len(pairs)}')
    print('nonzero@vote_epoch examples:')
    for row in nonzero_vote:
        print(row)
    print('nonzero@epoch examples:')
    for row in nonzero_epoch:
        print(row)
    print('nonzero@vote_epoch+week examples:')
    for row in nonzero_next:
        print(row)


if __name__ == '__main__':
    main()
