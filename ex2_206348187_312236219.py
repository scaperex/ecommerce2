import abc
from typing import Tuple
import pandas as pd
import numpy as np
import datetime
from scipy.sparse.linalg import lsqr
from scipy.sparse import lil_matrix


class Recommender(abc.ABC):
    def __init__(self, ratings: pd.DataFrame):
        self.initialize_predictor(ratings)

    @abc.abstractmethod
    def initialize_predictor(self, ratings: pd.DataFrame):
        raise NotImplementedError()

    @abc.abstractmethod
    def predict(self, user: int, item: int, timestamp: int) -> float:
        """
        :param user: User identifier
        :param item: Item identifier
        :param timestamp: Rating timestamp
        :return: Predicted rating of the user for the item
        """
        raise NotImplementedError()

    def rmse(self, true_ratings) -> float:
        """
        :param true_ratings: DataFrame of the real ratings
        :return: RMSE score
        """

        true_ratings['prediction'] = true_ratings.apply(lambda x: self.predict(user=int(x[0]), item=int(x[1]), timestamp=x[3]), axis=1)

        rmse = np.sqrt(np.mean((true_ratings['rating'] - true_ratings['prediction'])**2))
        return rmse

# runtime 1 minute max - BaselineRecommender + NeighborhoodRecommender - took 40.73s
class BaselineRecommender(Recommender):
    def initialize_predictor(self, ratings: pd.DataFrame):
        ratings = ratings.copy(deep=True)
        ratings.drop('timestamp', axis=1, inplace=True)
        self.R_hat = ratings.rating.mean()
        self.B_u = ratings.drop('item', axis=1).groupby(by='user').mean().rename(
            columns={'rating': 'user_rating_mean'})

        self.B_u['user_rating_mean'] -= self.R_hat
        self.B_i = ratings.drop('user', axis=1).groupby(by='item').mean().rename(
            columns={'rating': 'item_rating_mean'})
        self.B_i['item_rating_mean'] -= self.R_hat



    def predict(self, user: int, item: int, timestamp: int) -> float:
        """
        :param user: User identifier
        :param item: Item identifier
        :param timestamp: Rating timestamp
        :return: Predicted rating of the user for the item
        """
        try:
            prediction = self.R_hat + self.B_u.loc[user, 'user_rating_mean'] + self.B_i.loc[item, 'item_rating_mean']
        except Exception:
            prediction = self.R_hat
        return float(np.clip(prediction, a_min=0.5, a_max=5))

class NeighborhoodRecommender(Recommender):
    def initialize_predictor(self, ratings: pd.DataFrame):
        ratings = ratings.copy(deep=True)
        ratings.drop('timestamp', axis=1, inplace=True)
        self.R_hat = ratings.rating.mean()
        self.B_u = ratings.drop('item', axis=1).groupby(by='user').mean().rename(columns={'rating': 'user_rating_mean'})

        self.B_u['user_rating_mean'] -= self.R_hat
        self.B_i = ratings.drop('user', axis=1).groupby(by='item').mean().rename(columns={'rating': 'item_rating_mean'})
        self.B_i['item_rating_mean'] -= self.R_hat

        ratings['rating_adjusted'] = ratings['rating']-self.R_hat
        num_users = len(self.B_u)
        num_items = len(self.B_i)

        R_tilde = np.zeros((num_items, num_users))
        R_tilde[ratings.item.values.astype(int), ratings.user.values.astype(int)] = ratings.rating_adjusted.values
        self.R_tilde = pd.DataFrame(R_tilde).astype(pd.SparseDtype("float", 0.0))


        """
            Calculate Correlation by:
            R^T@R / (R**2@S)^T*(R**2@S)
        """
        self.binary_R_tilde = self.R_tilde != 0
        R_tilde_square = self.R_tilde ** 2
        a = R_tilde_square.transpose().dot(self.binary_R_tilde)
        denominator = a.transpose() * a
        nominator = self.R_tilde.transpose().dot(self.R_tilde)
        corr = nominator / np.sqrt(denominator)
        self.user_corr = corr.fillna(0)
        self.binary_R_tilde = self.binary_R_tilde.sparse.to_dense()
        self.R_tilde = self.R_tilde.sparse.to_dense()
        self.num_neighbours = 3

    def predict(self, user: int, item: int, timestamp: int) -> float:
        """
        :param user: User identifier
        :param item: Item identifier
        :param timestamp: Rating timestamp
        :return: Predicted rating of the user for the item
        """
        nearest_neighbours = self.binary_R_tilde.loc[item]*self.user_corr.loc[user]

        nearest_neighbours.loc[nearest_neighbours == 0] = None
        nearest_neighbours_corr = nearest_neighbours[nearest_neighbours.abs().nlargest(n=self.num_neighbours).index]

        nearest_neighbours_ratings = self.R_tilde.loc[int(item), nearest_neighbours_corr.index]
        nominator = (nearest_neighbours_corr*nearest_neighbours_ratings).sum()
        denominator = nearest_neighbours_corr.abs().sum()

        # Handle case where there are no neighbours with correlation
        if nominator == 0 or denominator == 0:# and denominator != -1 * float('inf'):
            neighbour_deviation = 0
        else:
            neighbour_deviation = nominator / denominator

        try:
            prediction = self.R_hat + self.B_u.loc[user, 'user_rating_mean'] + self.B_i.loc[item, 'item_rating_mean'] + neighbour_deviation
        except Exception:
            prediction = self.R_hat
        return float(np.clip(prediction, a_min=0.5, a_max=5))

    def user_similarity(self, user1: int, user2: int) -> float:
        """
        :param user1: User identifier
        :param user2: User identifier
        :return: The correlation of the two users (between -1 and 1)
        """
        corr = self.user_corr.loc[user1, user2]
        return corr

# runtime 3 minute max - LSRecommender took 0.53s
class LSRecommender(Recommender):
    def initialize_predictor(self, ratings: pd.DataFrame):
        ratings = ratings.copy(deep=True)
        ratings['date'] = pd.to_datetime(ratings['timestamp'], unit='s')
        ratings['weekday'] = pd.to_datetime(ratings['date']).dt.dayofweek  # monday = 0, sunday = 6
        ratings['is_weekend'] = 0
        ratings.loc[ratings['weekday'].isin([4, 5]), 'is_weekend'] = 1
        ratings['is_daytime'] = ratings['date'].dt.time.between(datetime.time(6, 00), datetime.time(18, 00))
        ratings['is_nighttime'] = ~ratings['is_daytime']

        self.R_hat = ratings.rating.mean()

        self.y = ratings['rating'] - self.R_hat
        ratings.drop(['timestamp', 'rating', 'date','weekday'], axis=1, inplace=True)
        ratings = ratings.astype(int)
        self.X = pd.get_dummies(ratings, columns=['user', 'item'], sparse=True)
        self.is_weekend_index = self.X.columns.get_loc('is_weekend')
        self.is_daytime_index = self.X.columns.get_loc('is_daytime')
        self.is_nighttime_index = self.X.columns.get_loc('is_nighttime')

    def predict(self, user: int, item: int, timestamp: int) -> float:
        """
        :param user: User identifier
        :param item: Item identifier
        :param timestamp: Rating timestamp
        :return: Predicted rating of the user for the item
        """
        try:
            # According to result of pd.get_dummies!
            user_index = self.X.columns.get_loc(f'user_{user}')
            item_index = self.X.columns.get_loc(f'item_{item}')
            indices = [user_index, item_index]

            date = datetime.datetime.fromtimestamp(timestamp)

            if date.weekday() in [4, 5]:
                indices.append(self.is_weekend_index)

            if date.time() > datetime.time(6, 00) and date.time() < datetime.time(18, 00):
                indices.append(self.is_daytime_index)
            else:
                indices.append(self.is_nighttime_index)

            prediction = self.R_hat + self.beta[indices].sum()
        except Exception:
            prediction = self.R_hat

        return float(np.clip(prediction, a_min=0.5, a_max=5))

    def solve_ls(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Creates and solves the least squares regression
        :return: Tuple of X, b, y such that b is the solution to min ||Xb-y||
        """
        self.beta, _, _, _ = np.linalg.lstsq(self.X, self.y, rcond=None)
        return (self.X, self.beta, self.y)

class CompetitionRecommender(Recommender):
    def initialize_predictor(self, ratings: pd.DataFrame):
        ratings = ratings.copy(deep=True)
        ratings['date'] = pd.to_datetime(ratings['timestamp'], unit='s')
        ratings['weekday'] = pd.to_datetime(ratings['date']).dt.dayofweek  # monday = 0, sunday = 6
        ratings['year'] = pd.to_datetime(ratings['date']).dt.year
        ratings['quarter'] = pd.to_datetime(ratings['date']).dt.quarter
        ratings['is_weekend'] = 0
        ratings.loc[ratings['weekday'].isin([4, 5]), 'is_weekend'] = 1
        ratings['is_daytime'] = ratings['date'].dt.time.between(datetime.time(6, 00), datetime.time(18, 00))
        ratings['is_nighttime'] = ~ratings['is_daytime']

        self.R_hat = ratings.rating.mean()

        self.y = ratings['rating'] - self.R_hat
        ratings.drop(['timestamp', 'rating', 'date', 'weekday'], axis=1, inplace=True)
        ratings = ratings.astype(int)
        self.X = pd.get_dummies(ratings, columns=['user', 'item', 'year', 'quarter'], sparse=True)
        self.is_weekend_index = self.X.columns.get_loc('is_weekend')
        self.is_daytime_index = self.X.columns.get_loc('is_daytime')
        self.is_nighttime_index = self.X.columns.get_loc('is_nighttime')
        self.X_scipy = CompetitionRecommender.data_frame_to_scipy_sparse_matrix(self.X)

        self.solve_ls()



    def solve_ls(self) -> None:
        """
        Creates and solves the least squares regression
        :return: Tuple of X, b, y such that b is the solution to min ||Xb-y||
        """
        self.beta = lsqr(self.X_scipy, self.y, damp=1.5, show=False)[0]


    def predict(self, user: int, item: int, timestamp: int) -> float:
        """
        :param user: User identifier
        :param item: Item identifier
        :param timestamp: Rating timestamp
        :return: Predicted rating of the user for the item
        """
        prediction = self.raw_predict(user, item, timestamp)
        prediction = float(np.clip(prediction, a_min=0.5, a_max=5))
        #prediction = round(prediction*2)/2
        return prediction

    def raw_predict(self, user: int, item: int, timestamp: int) -> float:
        try:
            # According to result of pd.get_dummies!
            user_index = self.X.columns.get_loc(f'user_{user}')
            indices = [user_index]
            try:
                item_index = self.X.columns.get_loc(f'item_{item}')
                indices.append(item_index)
            except KeyError as e:
                # print(f"KeyError:{e}")
                pass
            date = datetime.datetime.fromtimestamp(timestamp)
            indices.append(self.X.columns.get_loc(f'year_{date.year}'))
            indices.append(self.X.columns.get_loc(f'quarter_{(date.month-1)//3+1}'))
            if date.weekday() in [4, 5]:
                indices.append(self.is_weekend_index)

            if date.time() > datetime.time(6, 00) and date.time() < datetime.time(18, 00):
                indices.append(self.is_daytime_index)
            else:
                indices.append(self.is_nighttime_index)

            prediction = self.R_hat + self.beta[indices].sum()
        except:
            prediction = self.R_hat
        return prediction


    @staticmethod
    def data_frame_to_scipy_sparse_matrix(df):
        """
        Converts a sparse pandas data frame to sparse scipy csr_matrix.
        :param df: pandas data frame
        :return: csr_matrix
        """
        arr = lil_matrix(df.shape, dtype=np.float32)
        for i, col in enumerate(df.columns):
            ix = df[col] != 0
            arr[np.where(ix), i] = 1

        return arr.tocsr()
