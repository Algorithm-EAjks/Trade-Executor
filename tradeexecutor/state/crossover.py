"""Provides functions to detect crossovers.

- crossover between two series
- crossover between a series and a constant value
"""

import pandas as pd
import operator

    
def contains_cross_over(
        series1: pd.Series, 
        series2: pd.Series,
        lookback_period: int = 2,
        must_return_index: bool = False,
) -> bool:
    """Detect if two series have cross over. To be used in decide_trades() 
    
    :param series1:
        A pandas.Series object.
        
    :param series2:
        A pandas.Series object.

    :lookback_period:
        The number of periods to look back to detect a crossover.
    
    :param must_return_index:
        If True, also returns the index of the crossover.
        
    :returns:
        bool. True if the series has crossed over the other series in the latest iteration, False otherwise.
        If must_return_index is True, also returns the index of the crossover. Note the index is a negative index e.g. -1 is the latest index, -2 is the second latest etc.
        
    """

    return _cross_check(series1, series2, lookback_period, must_return_index, operator.gt, operator.lt)


def contains_cross_under(
        series1: pd.Series, 
        series2: pd.Series,
        lookback_period: int = 2,
        must_return_index: bool = False,
) -> bool:
    """Detect if two series have cross over. To be used in decide_trades() 
    
    :param series1:
        A pandas.Series object.
        
    :param series2:
        A pandas.Series object.

    :lookback_period:
        The number of periods to look back to detect a crossover.
    
    :param must_return_index:
        If True, also returns the index of the crossover.
        
    bool. True if the series has crossed under the other series in the latest iteration, False otherwise.
        If must_return_index is True, also returns the index of the crossover. Note the index is a negative index e.g. -1 is the latest index, -2 is the second latest etc.
        
    """

    return _cross_check(series1, series2, lookback_period, must_return_index, operator.lt, operator.gt)


def _cross_check(
    series1, 
    series2, 
    lookback_period, 
    must_return_index, 
    comparison_operator,
    unlock_operator,
):
    
    assert type(series1) == type(series2) == pd.Series, "Series must be pandas.Series"
    assert comparison_operator != unlock_operator, "comparison_operator and unlock_operator must be different"
    
    lookback1, lookback2 = _get_lookback(series1, series2, lookback_period)

    # get index of cross
    cross_index = None
    has_crossed = False
    locked = True
    
    for i, (x,y) in enumerate(zip(lookback1, lookback2)):
        
            if comparison_operator(x, y) and not locked:
                has_crossed = True
                cross_index = -(len(lookback1) - i)
                locked = True
                # don't break since we want to get the latest cross
            
            
            if unlock_operator(x, y):
                locked = False

    if must_return_index:
        return has_crossed, cross_index
    else:
        return has_crossed
    

def _get_lookback(series1, series2, lookback_period):
    
    if len(series1) < lookback_period or len(series2) < lookback_period:
        lookback_period = min(len(series1), len(series2))
    
    lookback1 = series1.iloc[-lookback_period:]
    lookback2 = series2.iloc[-lookback_period:]

    return lookback1,lookback2

    