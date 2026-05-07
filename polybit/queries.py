"""GraphQL queries for Bitquery's Prediction Market API (Polygon / Polymarket).

Field shapes are taken from the official docs:
- https://docs.bitquery.io/docs/examples/prediction-market/prediction-trades-api/
- https://docs.bitquery.io/docs/examples/prediction-market/prediction-managements-api/
"""

from __future__ import annotations


# Streaming subscription: every successful Polymarket trade with the fields
# we need for matching (CollateralAmountInUSD, Price, Buyer/Seller),
# display (Title, Image, Outcome.Label) and links (Transaction.Hash, MarketId).
TRADES_SUBSCRIPTION = """
subscription PolymarketTradesStream {
  EVM(network: matic) {
    PredictionTrades(
      where: {
        TransactionStatus: {Success: true}
        Trade: {Prediction: {Marketplace: {ProtocolName: {is: "polymarket"}}}}
      }
    ) {
      Block { Time }
      Transaction { Hash From }
      Trade {
        OutcomeTrade {
          Buyer
          Seller
          Amount
          CollateralAmount
          CollateralAmountInUSD
          Price
          PriceInUSD
          IsOutcomeBuy
        }
        Prediction {
          ConditionId
          Question {
            Id
            Title
            MarketId
            Image
            CreatedAt
          }
          Outcome {
            Label
            Index
          }
          OutcomeToken {
            AssetId
          }
        }
      }
    }
  }
}
"""


# Top markets within the configured lookback window, by total USD volume
# (sum of CollateralAmountInUSD). Returns up to TOP_MARKETS_LIMIT rows; we
# paginate client-side.
TOP_MARKETS_BY_VOLUME = """
query TopMarketsByVolume($hours: Int!, $limit: Int!) {
  EVM(network: matic) {
    PredictionTrades(
      limit: {count: $limit}
      orderBy: {descendingByField: "volume_usd"}
      where: {
        TransactionStatus: {Success: true}
        Block: {Time: {since_relative: {hours_ago: $hours}}}
        Trade: {Prediction: {Marketplace: {ProtocolName: {is: "polymarket"}}}}
      }
    ) {
      Trade {
        Prediction {
          ConditionId
          Question {
            Id
            Title
            MarketId
            Image
          }
        }
      }
      volume_usd: sum(of: Trade_OutcomeTrade_CollateralAmountInUSD)
      trade_count: count
      unique_buyers: count(distinct: Trade_OutcomeTrade_Buyer)
    }
  }
}
"""


# Top markets within the configured lookback window, by unique trader count (using distinct Buyer addresses
# as a proxy; combining Buyer+Seller distinctness across one query is not
# directly supported by Bitquery's aggregation surface).
TOP_MARKETS_BY_TRADERS = """
query TopMarketsByTraders($hours: Int!, $limit: Int!) {
  EVM(network: matic) {
    PredictionTrades(
      limit: {count: $limit}
      orderBy: {descendingByField: "unique_buyers"}
      where: {
        TransactionStatus: {Success: true}
        Block: {Time: {since_relative: {hours_ago: $hours}}}
        Trade: {Prediction: {Marketplace: {ProtocolName: {is: "polymarket"}}}}
      }
    ) {
      Trade {
        Prediction {
          ConditionId
          Question {
            Id
            Title
            MarketId
            Image
          }
        }
      }
      volume_usd: sum(of: Trade_OutcomeTrade_CollateralAmountInUSD)
      trade_count: count
      unique_buyers: count(distinct: Trade_OutcomeTrade_Buyer)
    }
  }
}
"""


# Top markets within the configured lookback window, by raw trade count.
TOP_MARKETS_BY_TRADES = """
query TopMarketsByTrades($hours: Int!, $limit: Int!) {
  EVM(network: matic) {
    PredictionTrades(
      limit: {count: $limit}
      orderBy: {descendingByField: "trade_count"}
      where: {
        TransactionStatus: {Success: true}
        Block: {Time: {since_relative: {hours_ago: $hours}}}
        Trade: {Prediction: {Marketplace: {ProtocolName: {is: "polymarket"}}}}
      }
    ) {
      Trade {
        Prediction {
          ConditionId
          Question {
            Id
            Title
            MarketId
            Image
          }
        }
      }
      volume_usd: sum(of: Trade_OutcomeTrade_CollateralAmountInUSD)
      trade_count: count
      unique_buyers: count(distinct: Trade_OutcomeTrade_Buyer)
    }
  }
}
"""


# Most recently created markets (no time filter — we just take the latest N).
#
# Currently DORMANT in the bot UI (see BitqueryHTTP.new_markets docstring),
# but kept here because the query is correct and re-wiring is one config
# change away.
#
# We intentionally DON'T filter by Marketplace.ProtocolName here. On
# `EVM(network: matic)` the PredictionManagements feed is already
# Polymarket-only in practice, and adding the protocol filter caused
# Bitquery to return zero rows (likely a schema/indexing quirk on the
# Management side).
NEW_MARKETS = """
query NewMarkets($limit: Int!) {
  EVM(network: matic) {
    PredictionManagements(
      limit: {count: $limit}
      orderBy: {descending: Block_Time}
      where: { Management: {Prediction:{Marketplace:{ProtocolName:{is:"polymarket"}}} EventType: { is: "Created" } } }
    ) {
      Block {
        Time
      }
      Management {
        Prediction {
          Question {
            Id
            Title
            MarketId
            Image
            CreatedAt
          }
          Condition {
            Outcomes {
              Label
              Index
            }
            Id
            QuestionId
          }
          Outcome {
            Id
            Index
            Label
          }
          OutcomeToken {
            Name
            SmartContract
            Symbol
            AssetId
          }
          Marketplace {
            SmartContract
            ProtocolName
          }
        }
        EventType
        Description
      }
    }
  }
}
"""


# Search markets by title substring within the configured lookback window.
SEARCH_MARKETS = """
query SearchMarkets($q: String!, $limit: Int!, $hours: Int!) {
  EVM(network: matic) {
    PredictionTrades(
      limit: {count: $limit}
      orderBy: {descendingByField: "volume_usd"}
      where: {
        TransactionStatus: {Success: true}
        Block: {Time: {since_relative: {hours_ago: $hours}}}
        Trade: {
          Prediction: {
            Marketplace: {ProtocolName: {is: "polymarket"}}
            Question: {Title: {includesCaseInsensitive: $q}}
          }
        }
      }
    ) {
      Trade {
        Prediction {
          ConditionId
          Question {
            Id
            Title
            MarketId
            Image
          }
        }
      }
      volume_usd: sum(of: Trade_OutcomeTrade_CollateralAmountInUSD)
      trade_count: count
      unique_buyers: count(distinct: Trade_OutcomeTrade_Buyer)
    }
  }
}
"""


# Latest price per outcome across a set of markets (one row per OutcomeToken
# AssetId restricted to the given market IDs). Used to enrich the
# Top/New/Search list views with current Yes/No prices in one request.
CURRENT_PRICES_FOR_MARKETS = """
query CurrentPricesForMarkets($marketIds: [String!]!) {
  EVM(network: matic) {
    PredictionTrades(
      limitBy: {by: Trade_Prediction_OutcomeToken_AssetId, count: 1}
      orderBy: {descending: Block_Time}
      where: {
        TransactionStatus: {Success: true}
        Trade: {
          Prediction: {Question: {MarketId: {in: $marketIds}}}
        }
      }
    ) {
      Block { Time }
      Trade {
        OutcomeTrade {
          Price(maximum: Block_Time)
          PriceInUSD(maximum: Block_Time)
        }
        Prediction {
          Question { MarketId }
          Outcome { Label Index }
          OutcomeToken { AssetId }
        }
      }
    }
  }
}
"""


# Volume / trade count per market within the configured window (for enriching New Markets where
# volume isn't part of the original PredictionManagements query).
VOLUMES_FOR_MARKETS = """
query VolumesForMarkets($marketIds: [String!]!, $hours: Int!) {
  EVM(network: matic) {
    PredictionTrades(
      where: {
        TransactionStatus: {Success: true}
        Block: {Time: {since_relative: {hours_ago: $hours}}}
        Trade: {Prediction: {Question: {MarketId: {in: $marketIds}}}}
      }
    ) {
      Trade { Prediction { Question { MarketId } } }
      volume_usd: sum(of: Trade_OutcomeTrade_CollateralAmountInUSD)
      trade_count: count
      unique_buyers: count(distinct: Trade_OutcomeTrade_Buyer)
    }
  }
}
"""


# Latest price per outcome for ONE market (used by the market detail card).
CURRENT_PRICES_FOR_MARKET = """
query CurrentPricesForMarket($marketId: String!) {
  EVM(network: matic) {
    PredictionTrades(
      limitBy: {by: Trade_Prediction_OutcomeToken_AssetId, count: 1}
      orderBy: {descending: Block_Time}
      where: {
        TransactionStatus: {Success: true}
        Trade: {
          Prediction: {Question: {MarketId: {is: $marketId}}}
        }
      }
    ) {
      Block { Time }
      Trade {
        OutcomeTrade {
          Price(maximum: Block_Time)
          PriceInUSD(maximum: Block_Time)
        }
        Prediction {
          ConditionId
          Question { Id Title Image MarketId }
          Outcome { Label Index }
          OutcomeToken { AssetId }
        }
      }
    }
  }
}
"""
