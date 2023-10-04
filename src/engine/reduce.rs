use ndarray::prelude::*;

use cfg_if::cfg_if;
use differential_dataflow::{
    difference::{Multiply, Semigroup},
    ExchangeData,
};
use ordered_float::OrderedFloat;
use serde::{Deserialize, Serialize};
use std::cmp::Reverse;
use std::iter::repeat;
use std::num::NonZeroUsize;
use std::ops::Add;

use super::{Key, Value};

#[derive(Debug, Clone, Copy)]
pub enum Reducer {
    FloatSum,
    IntSum,
    ArraySum,
    Unique,
    Min,
    ArgMin,
    Max,
    ArgMax,
    SortedTuple(bool),
    Tuple(bool),
    Any,
}

pub trait SemigroupReducerImpl: 'static {
    type State: ExchangeData + Semigroup + Multiply<isize>;

    fn init(&self, key: &Key, value: &Value) -> Option<Self::State>;

    fn finish(&self, state: Self::State) -> Value;
}

pub trait ReducerImpl: 'static {
    type State: ExchangeData;

    fn init(&self, key: &Key, values: &[Value]) -> Option<Self::State>;

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State;

    fn finish(&self, state: Self::State) -> Value;
}

pub trait UnaryReducerImpl: 'static {
    type State: ExchangeData;

    fn init_unary(&self, key: &Key, value: &Value) -> Option<Self::State>;

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State;

    fn finish(&self, state: Self::State) -> Value;
}

impl<T: UnaryReducerImpl> ReducerImpl for T {
    type State = <Self as UnaryReducerImpl>::State;
    fn init(&self, key: &Key, values: &[Value]) -> Option<Self::State> {
        self.init_unary(key, &values[0])
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> <Self as UnaryReducerImpl>::State {
        <Self as UnaryReducerImpl>::combine(self, values)
    }

    fn finish(&self, state: Self::State) -> Value {
        <Self as UnaryReducerImpl>::finish(self, state)
    }
}

#[derive(Debug, Clone, Hash, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct IntSumState {
    count: isize,
    sum: i64,
}

impl Semigroup for IntSumState {
    fn is_zero(&self) -> bool {
        self.count.is_zero() && self.sum.is_zero()
    }

    fn plus_equals(&mut self, rhs: &Self) {
        self.count.plus_equals(&rhs.count);
        self.sum.plus_equals(&rhs.sum);
    }
}

impl Multiply<isize> for IntSumState {
    type Output = Self;
    fn multiply(self, rhs: &isize) -> Self::Output {
        let count = self.count * rhs;
        let sum = self.sum * i64::try_from(*rhs).unwrap();
        Self { count, sum }
    }
}

impl IntSumState {
    pub fn single(val: i64) -> Self {
        Self { count: 1, sum: val }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct IntSumReducer;

impl SemigroupReducerImpl for IntSumReducer {
    type State = IntSumState;

    fn init(&self, _key: &Key, value: &Value) -> Option<Self::State> {
        match value {
            Value::Int(i) => Some(IntSumState::single(*i)),
            _ => panic!("unsupported type for int_sum"),
        }
    }

    fn finish(&self, state: Self::State) -> Value {
        Value::Int(state.sum)
    }
}
#[derive(Debug, Clone, Copy)]
pub struct FloatSumReducer;

impl UnaryReducerImpl for FloatSumReducer {
    type State = OrderedFloat<f64>;

    fn init_unary(&self, _key: &Key, value: &Value) -> Option<Self::State> {
        match value {
            Value::Float(f) => Some(*f),
            _ => None,
        }
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        #[allow(clippy::cast_precision_loss)]
        values
            .into_iter()
            .map(|(value, cnt)| *value * cnt.get() as f64)
            .sum()
    }

    fn finish(&self, state: Self::State) -> Value {
        Value::Float(state)
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ArraySumReducer;

#[derive(Debug)]
enum ArraySumState<'a> {
    IntArray(CowArray<'a, i64, IxDyn>),
    FloatArray(CowArray<'a, f64, IxDyn>),
}

impl<'a> ArraySumState<'a> {
    fn new(value: &'a Value, cnt: NonZeroUsize) -> Self {
        match value {
            #[allow(clippy::cast_precision_loss)]
            Value::IntArray(array) => {
                if cnt.get() == 1 {
                    Self::IntArray(CowArray::from(&**array))
                } else {
                    Self::IntArray(CowArray::from(&**array * i64::try_from(cnt.get()).unwrap()))
                }
            }
            #[allow(clippy::cast_precision_loss)]
            Value::FloatArray(array) => {
                if cnt.get() == 1 {
                    Self::FloatArray(CowArray::from(&**array))
                } else {
                    Self::FloatArray(CowArray::from(&**array * cnt.get() as f64))
                }
            }
            _ => panic!("unsupported type for npsum"),
        }
    }
}

impl<'a> Add for ArraySumState<'a> {
    type Output = Self;

    fn add(self, rhs: Self) -> Self {
        match (self, rhs) {
            (Self::IntArray(lhs), Self::IntArray(rhs)) => {
                Self::IntArray(CowArray::from(lhs.into_owned() + &rhs))
            }
            (Self::FloatArray(lhs), Self::FloatArray(rhs)) => {
                Self::FloatArray(CowArray::from(lhs.into_owned() + &rhs))
            }
            _ => panic!("mixing types in npsum is not allowed"),
        }
    }
}

impl<'a> From<ArraySumState<'a>> for Value {
    fn from(state: ArraySumState<'a>) -> Self {
        match state {
            ArraySumState::IntArray(a) => Self::from(a.into_owned()),
            ArraySumState::FloatArray(a) => Self::from(a.into_owned()),
        }
    }
}

impl UnaryReducerImpl for ArraySumReducer {
    type State = Value;

    fn init_unary(&self, _key: &Key, value: &Value) -> Option<Self::State> {
        Some(value.clone())
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .map(|(value, cnt)| ArraySumState::new(value, cnt))
            .reduce(|a, b| a + b)
            .expect("values should not be empty")
            .into()
    }

    fn finish(&self, state: Self::State) -> Value {
        state
    }
}

#[derive(Debug, Clone, Copy)]
pub struct UniqueReducer;

impl UnaryReducerImpl for UniqueReducer {
    type State = Option<Value>;

    fn init_unary(&self, _key: &Key, value: &Value) -> Option<Self::State> {
        Some(Some(value.clone()))
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        let mut values = values.into_iter();
        let (state, _cnt) = values.next().unwrap();
        assert!(
            values.next().is_none(),
            "More than one distinct value passed to the unique reducer."
        );
        state.clone()
    }

    fn finish(&self, state: Self::State) -> Value {
        match state {
            Some(v) => v,
            None => Value::None,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct MinReducer;

impl UnaryReducerImpl for MinReducer {
    type State = Value;

    fn init_unary(&self, _key: &Key, value: &Value) -> Option<Self::State> {
        Some(value.clone())
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .map(|(val, _cnt)| val)
            .min()
            .unwrap()
            .clone()
    }

    fn finish(&self, state: Self::State) -> Value {
        state
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ArgMinReducer;

impl UnaryReducerImpl for ArgMinReducer {
    type State = (Value, Key);

    fn init_unary(&self, key: &Key, value: &Value) -> Option<Self::State> {
        Some((value.clone(), *key))
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .map(|(val, _cnt)| val)
            .min()
            .unwrap()
            .clone()
    }

    fn finish(&self, state: Self::State) -> Value {
        Value::Pointer(state.1)
    }
}

cfg_if! {
    if #[cfg(feature="yolo-id32")] {
        const SALT: u32 = 0xDE_AD_BE_EF_u32;
    } else if #[cfg(feature="yolo-id64")] {
        const SALT: u64 = 0xDE_AD_BE_EF_DE_AD_BE_EF_u64;
    } else {
        const SALT: u128 = 0xDE_AD_BE_EF_DE_AD_BE_EF_DE_AD_BE_EF_DE_AD_BE_EF_u128;
    }
}

pub struct AnyReducer;

impl UnaryReducerImpl for AnyReducer {
    type State = (Key, Value);

    fn init_unary(&self, key: &Key, value: &Value) -> Option<Self::State> {
        Some((*key, value.clone()))
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .map(|(val, _cnt)| val)
            .min_by_key(|(key, value)| (key.salted_with(SALT), value))
            .unwrap()
            .clone()
    }

    fn finish(&self, state: Self::State) -> Value {
        state.1
    }
}

#[derive(Debug, Clone, Copy)]
pub struct MaxReducer;

impl UnaryReducerImpl for MaxReducer {
    type State = Value;

    fn init_unary(&self, _key: &Key, value: &Value) -> Option<Self::State> {
        Some(value.clone())
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .map(|(val, _cnt)| val)
            .max()
            .unwrap()
            .clone()
    }

    fn finish(&self, state: Self::State) -> Value {
        state
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ArgMaxReducer;

impl UnaryReducerImpl for ArgMaxReducer {
    type State = (Value, Key);

    fn init_unary(&self, key: &Key, value: &Value) -> Option<Self::State> {
        Some((value.clone(), *key))
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .map(|(val, _cnt)| val)
            .max_by_key(|(value, key)| (value, Reverse(key)))
            .unwrap()
            .clone()
    }

    fn finish(&self, state: Self::State) -> Value {
        Value::Pointer(state.1)
    }
}

#[derive(Debug, Clone, Copy)]
pub struct SortedTupleReducer {
    skip_nones: bool,
}

impl SortedTupleReducer {
    pub fn new(skip_nones: bool) -> Self {
        Self { skip_nones }
    }
}

impl UnaryReducerImpl for SortedTupleReducer {
    type State = Vec<Value>;

    fn init_unary(&self, _key: &Key, value: &Value) -> Option<Self::State> {
        if *value == Value::None && self.skip_nones {
            Some(vec![])
        } else {
            Some(vec![value.clone()])
        }
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .flat_map(|(state, cnt)| {
                state
                    .iter()
                    .flat_map(move |v| repeat(v).take(cnt.get()))
                    .cloned()
            })
            .collect()
    }

    fn finish(&self, mut state: Self::State) -> Value {
        state.sort();
        state.as_slice().into()
    }
}

#[derive(Debug, Clone, Copy)]
pub struct TupleReducer {
    skip_nones: bool,
}

impl TupleReducer {
    pub fn new(skip_nones: bool) -> Self {
        Self { skip_nones }
    }
}

impl ReducerImpl for TupleReducer {
    type State = Vec<(Option<Value>, Key, Value)>;

    fn init(&self, key: &Key, values: &[Value]) -> Option<Self::State> {
        if values[0] == Value::None && self.skip_nones {
            Some(vec![])
        } else if values.len() > 1 {
            Some(vec![(Some(values[1].clone()), *key, values[0].clone())])
        } else {
            Some(vec![(None, *key, values[0].clone())])
        }
    }

    fn combine<'a>(
        &self,
        values: impl IntoIterator<Item = (&'a Self::State, NonZeroUsize)>,
    ) -> Self::State {
        values
            .into_iter()
            .flat_map(|(state, cnt)| {
                state
                    .iter()
                    .flat_map(move |v| repeat(v).take(cnt.get()))
                    .cloned()
            })
            .collect()
    }

    fn finish(&self, mut state: Self::State) -> Value {
        state.sort();
        state
            .into_iter()
            .map(|(_, _, value)| value)
            .collect::<Vec<Value>>()
            .as_slice()
            .into()
    }
}
